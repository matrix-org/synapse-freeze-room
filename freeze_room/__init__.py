# -*- coding: utf-8 -*-
# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import logging
from typing import List, Tuple, Optional, Set, Iterable

import attr
from frozendict import frozendict
from synapse.events import EventBase
from synapse.module_api import ModuleApi, UserID
from synapse.types import StateMap

from freeze_room._constants import EventTypes, Membership

logger = logging.getLogger(__name__)

FROZEN_STATE_TYPE = "org.matrix.room.frozen"


@attr.s(auto_attribs=True, frozen=True)
class FreezeRoomConfig:
    unfreeze_blacklist: List[str] = []
    promote_moderators: bool = False


class FreezeRoom:
    def __init__(self, config: FreezeRoomConfig, api: ModuleApi):
        self._api = api
        self._config = config

        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
        )

    @staticmethod
    def parse_config(config: dict) -> FreezeRoomConfig:
        return FreezeRoomConfig(
            config.get("unfreeze_blacklist", []),
            config.get("promote_moderators", False),
        )

    async def check_event_allowed(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        """Implements synapse.events.ThirdPartyEventRules.check_event_allowed.

        Checks the event's type and the current rule and calls the right function to
        determine whether the event can be allowed.

        Args:
            event: The event to check.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room the event originated from.

        Returns:
            True if the event should be allowed, False if it should be rejected. If the
            event should be allowed but with some of its data replaced (or its context
            needs to be recalculated, eg because the state of the room has changed), a
            dictionary might be returned in addition to the boolean.
        """
        if event.type == FROZEN_STATE_TYPE and event.is_state():
            return await self._on_frozen_state_change(event, state_events)

        # If the room is frozen, we allow a very small number of events to go through
        # (unfreezing, leaving, etc.).
        frozen_state = state_events.get((FROZEN_STATE_TYPE, ""))
        if frozen_state and frozen_state.content.get("frozen", False) is True:
            return await self._on_event_when_frozen(event, state_events)

        # If the event is a leave membership update, check if the last admin is leaving
        # the room and freeze it if not.
        if (
            event.type == EventTypes.Member
            and event.membership == Membership.LEAVE
            and event.is_state()
        ):
            await self._on_room_leave(event, state_events)

        return True, None

    async def _on_frozen_state_change(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        frozen = event.content.get("frozen", None)
        if not isinstance(frozen, bool):
            # Invalid event: frozen is either missing or not a boolean.
            return False, None

        # If a user on the unfreeze blacklist attempts to unfreeze the room, don't allow
        # the state change.
        if (
            frozen is False
            and UserID.from_string(event.sender).domain in self._config.unfreeze_blacklist
        ):
            return False, None

        current_frozen_state = state_events.get(
            (FROZEN_STATE_TYPE, ""),
        )  # type: EventBase

        if (
            current_frozen_state is not None
            and current_frozen_state.content.get("frozen") == frozen
        ):
            # This is a noop, accept the new event but don't do anything more.
            return True, None

        # If the event was received over federation, we want to accept it but not to
        # change the power levels.
        if not self._is_local_user(event.sender):
            return True, None

        current_join_rules = state_events.get(
            (EventTypes.JoinRules, ""),
        )  # type: EventBase

        # If the room is publicly joinable, revert that upon freezing the room.
        if frozen is True and(
            current_join_rules is None
            or current_join_rules.content["join_rule"] == "public"
        ):
            await self._api.create_and_send_event_into_room(
                {
                    "room_id": event.room_id,
                    "sender": event.sender,
                    "type": EventTypes.JoinRules,
                    "content": {"join_rule": "invite"},
                    "state_key": "",
                }
            )

        current_power_levels = state_events.get(
            (EventTypes.PowerLevels, ""),
        )  # type: EventBase

        power_levels_content = unfreeze(current_power_levels.content)

        if not frozen:
            # We're unfreezing the room: enforce the right value for the power levels so
            # the room isn't in a weird/broken state afterwards.
            users = power_levels_content.setdefault("users", {})
            users[event.sender] = 100
            power_levels_content["users_default"] = 0
        else:
            # Send a new power levels event with a similar content to the previous one
            # except users_default is 100 to allow any user to unfreeze the room.
            power_levels_content["users_default"] = 100

            # Just to be safe, also delete all users that don't have a power level of
            # 100, in order to prevent anyone from being unable to unfreeze the room.
            users = {}
            for user, level in power_levels_content["users"].items():
                if level == 100:
                    users[user] = level
            power_levels_content["users"] = users

        await self._api.create_and_send_event_into_room(
            {
                "room_id": event.room_id,
                "sender": event.sender,
                "type": EventTypes.PowerLevels,
                "content": power_levels_content,
                "state_key": "",
            }
        )

        return True, event.get_dict()

    async def _on_event_when_frozen(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        """Check if the provided event is allowed when the room is frozen.

        The only events allowed are for a member to leave the room, and for the room to
        be (un)frozen. In the latter case, also attempt to unfreeze the room.


        Args:
            event: The event to allow or deny.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room before the event was sent.
        Returns:
            A boolean indicating whether the event is allowed, or a dict if the event is
            allowed but the state of the room has been modified (i.e. the room has been
            unfrozen). This is because returning a dict of the event forces Synapse to
            rebuild it, which is needed if the state of the room has changed.
        """
        # Allow users to leave the room; don't allow kicks though.
        if (
            event.type == EventTypes.Member
            and event.membership == Membership.LEAVE
            and event.sender == event.state_key
        ):
            return True, None

        if event.type == EventTypes.PowerLevels:
            # Check if the power level event is associated with a room unfreeze (because
            # the power level events will be sent before the frozen state event). This
            # means we check that the users_default is back to 0 and the sender set
            # themselves as admin.
            current_power_levels = state_events.get((EventTypes.PowerLevels, ""))
            if current_power_levels:
                old_content = current_power_levels.content.copy()
                old_content["users_default"] = 0

                new_content = unfreeze(event.content)
                sender_pl = new_content.get("users", {}).get(event.sender, 0)

                # We don't care about the users section as long as the new event gives
                # full power to the sender.
                del old_content["users"]
                del new_content["users"]

                if new_content == old_content and sender_pl == 100:
                    return True, None

        return False, None

    async def _on_room_leave(
        self, event: EventBase, state_events: StateMap[EventBase],
    ) -> None:
        """React to a m.room.member event with a "leave" membership.

        Checks if the user leaving the room is the last admin in the room. If so, checks
        if there are users with lower but non-default power levels that can be promoted
        to admins. If so, promotes them to admin if the configuration allows it,
        otherwise freezes the room.

        Args:
            event: The event to check.
            state_events: The current state of the room.
        """
        # Check if the last admin is leaving the room.
        pl_content = _get_power_levels_content_from_state(state_events)
        if pl_content is None:
            return

        last_admin_leaving = _is_last_admin_leaving(event, pl_content, state_events)
        if not last_admin_leaving:
            return

        # If so, search for users to promote if the configuration allows it.
        if self._config.promote_moderators:
            # Look for users to promote.
            users_to_promote = _get_users_with_highest_nondefault_pl(
                pl_content["users"],
                pl_content.get("users_default", 0),
                state_events,
                ignore_user=event.state_key,
            )

            # If we found users to promote, update the power levels event in the room's
            # state.
            if users_to_promote:
                logger.info(
                    "Promoting users to admins in room %s: %s",
                    event.room_id,
                    users_to_promote,
                )
                await self._promote_to_admins(users_to_promote, pl_content, event)
                return

        # If not, freeze the room by marking it as frozen. We don't need to update the
        # power levels now as they'll get updated by on_frozen_state_change when this
        # event gets processed.
        logger.info("Freezing room %s", event.room_id)
        await self._api.create_and_send_event_into_room(
            {
                "room_id": event.room_id,
                "sender": event.sender,
                "type": FROZEN_STATE_TYPE,
                "content": {"frozen": True},
                "state_key": "",
            }
        )

        return

    async def _promote_to_admins(
        self,
        users_to_promote: Iterable[str],
        pl_content: dict,
        event: EventBase,
    ) -> None:
        """Promotes a given list of users to admins.

        Args:
            users_to_promote: The users to promote.
            pl_content: The content of the m.room.power_levels event that's currently in
                the room state.
            event: The event we want to use the sender and room_id of to send the new
                power levels event.
        """
        # Make a deep copy of the content so we don't edit the "users" dict from
        # the event that's currently in the room's state.
        new_pl_content = copy.deepcopy(pl_content)
        for user in users_to_promote:
            new_pl_content["users"][user] = pl_content["users"][event.sender]

        await self._api.create_and_send_event_into_room(
            {
                "room_id": event.room_id,
                "sender": event.sender,
                "type": EventTypes.PowerLevels,
                "content": new_pl_content,
                "state_key": "",
            }
        )

    def _is_local_user(self, user_id: str) -> bool:
        """Checks whether a given user ID belongs to this homeserver, or a remote

        Args:
            user_id: A user ID to check.

        Returns:
            True if the user belongs to this homeserver, False otherwise.
        """
        user = UserID.from_string(user_id)

        # Extract the localpart and ask the module API for a user ID from the localpart
        # The module API will append the local homeserver's server_name
        local_user_id = self._api.get_qualified_user_id(user.localpart)

        # If the user ID we get based on the localpart is the same as the original user
        # ID, then they were a local user
        return user_id == local_user_id


def _is_last_admin_leaving(
    event: EventBase,
    power_level_content: dict,
    state_events: StateMap[EventBase],
) -> bool:
    """Checks if the provided leave event is the last admin in the room leaving it.

    Args:
        event: The leave event to check.
        power_level_content: The content of the power levels event that's currently in
            the room's state.
        state_events: The current state of the room, from which we can check the room's
            member list.

    Returns:
        Whether this event is the last admin leaving the room.
    """
    # Get every admin user defined in the room's state
    admin_users = {
        user
        for user, power_level in power_level_content["users"].items()
        if power_level >= 100
    }

    if event.sender not in admin_users:
        # This user is not an admin, ignore them
        return False

    if any(
        event_type == EventTypes.Member
        and event.membership in [Membership.JOIN, Membership.INVITE]
        and state_key in admin_users
        and state_key != event.sender
        for (event_type, state_key), event in state_events.items()
    ):
        # There's another admin user in, or invited to, the room
        return False

    return True


def _get_power_levels_content_from_state(
    state_events: StateMap[EventBase],
) -> Optional[dict]:
    """Extracts the content of the power levels content from the provided set of state
    events. If the event has no "users" key, or there is no power levels event in the
    state of the room, None is returned instead.

    Args:
        state_events: The state events to extract power levels from.

    Returns:
        A dict representing the content of the power levels event, or None if no power
        levels event exist in the given state events or if one exists but its content is
        missing a "users" key.
    """
    power_level_state_event = state_events.get(
        (EventTypes.PowerLevels, "")
    )  # type: EventBase
    if not power_level_state_event:
        return None
    power_level_content = power_level_state_event.content

    # Do some validation checks on the power level state event
    if (
        not isinstance(power_level_content, dict)
        or "users" not in power_level_content
        or not isinstance(power_level_content["users"], dict)
    ):
        # We can't use this power level event to determine whether the room should be
        # frozen. Bail out.
        return None

    return power_level_content


def _get_users_with_highest_nondefault_pl(
    users_dict: dict,
    users_default_pl: int,
    state_events: StateMap[EventBase],
    ignore_user: str,
) -> Tuple[str]:
    """Looks at the provided bits of power levels event content to figure out what the
    maximum user-specific non-default power level is with users still in the room (or
    invited to it) and which users have it.

    Args:
        users_dict: The "users" dictionary from the power levels event content.
        users_default_pl: The default power level for users who don't appear in the users
            dictionary.
        state_events: The current state of the room, from which we can check the room's
            member list.
        ignore_user: A user to ignore, i.e. to consider they've left the room even if the
            room's state says otherwise.

    Returns:
        A tuple of users with the highest non-default power level, or an empty set if no
        such users exist in the room.
    """
    # Make a copy of the users dict so we don't modify the actual event content.
    users_dict_copy = users_dict.copy()

    if ignore_user in users_dict_copy:
        del users_dict_copy[ignore_user]

    while True:
        # If there's no more user to evaluate, return an empty tuple.
        if not users_dict_copy:
            return ()

        # Get the max power level in the dict.
        max_pl = max(users_dict_copy.values())

        # Bail out if the max power level is the default one (or is lower).
        if max_pl <= users_default_pl:
            return ()

        # Figure out which users have that maximum power level.
        users_with_max_pl = tuple(
            user_id
            for user_id, pl in users_dict_copy.items()
            if pl == max_pl
        )

        # Among those users, figure out which ones are still in the room (or have a
        # pending invite to it): those are the users we need to promote.
        users_to_promote = tuple(
            user_id
            for user_id in users_with_max_pl
            if (
                _get_membership(user_id, state_events)
                in [Membership.JOIN, Membership.INVITE]
            )
        )

        # If we've got users in the room to promote, break out and return.
        if users_to_promote:
            return users_to_promote

        # Otherwise, remove the users we've considered and start again.
        for user_id in users_with_max_pl:
            del users_dict_copy[user_id]


def _get_membership(
    user_id: str,
    state_events: StateMap[EventBase],
) -> Optional[Membership]:
    evt: Optional[EventBase] = state_events.get((EventTypes.Member, user_id))

    if evt is None:
        return None

    return evt.membership


def unfreeze(o):
    if isinstance(o, (dict, frozendict)):
        return {k: unfreeze(v) for k, v in o.items()}

    if isinstance(o, (bytes, str)):
        return o

    try:
        return [unfreeze(i) for i in o]
    except TypeError:
        pass

    return o

