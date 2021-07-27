from typing import List, Tuple, Optional

from synapse.events import EventBase
from synapse.module_api import ModuleApi, UserID
from synapse.types import StateMap
from synapse.util.frozenutils import unfreeze

from freeze_room._constants import EventTypes, Membership

FROZEN_STATE_TYPE = "org.matrix.room.frozen"


class RoomFreeze:
    def __init__(self, config: dict, api: ModuleApi):
        self._api = api
        self._unfreeze_blacklist: List[str] = config.get("unfreeze_blacklist", [])

        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
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
            True if the event should be allowed, False if it should be rejected, or a
            dictionary if the event needs to be rebuilt (containing the event's new
            content).
        """
        if event.type == FROZEN_STATE_TYPE:
            return await self._on_frozen_state_change(event, state_events)

        # If the room is frozen, we allow a very small number of events to go through
        # (unfreezing, leaving, etc.).
        frozen_state = state_events.get((FROZEN_STATE_TYPE, ""))
        if frozen_state and frozen_state.content.get("frozen", False):
            return await self._on_event_when_frozen(event, state_events)

    async def _on_frozen_state_change(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        frozen = event.content.get("frozen", None)
        if not isinstance(frozen, bool):
            # Invalid event: frozen is either missing or not a boolean.
            return False, None

        # If the event was sent from a restricted homeserver, don't allow the state
        # change.
        if UserID.from_string(event.sender).domain in self._unfreeze_blacklist:
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
