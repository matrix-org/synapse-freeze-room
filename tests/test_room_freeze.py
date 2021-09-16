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

# From Python 3.8 onwards, aiounittest.AsyncTestCase can be replaced by
# unittest.IsolatedAsyncioTestCase, so we'll be able to get rid of this dependency when
# we stop supporting Python < 3.8 in Synapse.
import aiounittest
from synapse.api.room_versions import RoomVersions
from synapse.events import FrozenEventV3

from freeze_room import FROZEN_STATE_TYPE, EventTypes, Membership, FreezeRoom
from tests import create_module


class RoomFreezeTest(aiounittest.AsyncTestCase):
    def setUp(self):
        self.user_id = "@alice:example.com"
        self.left_user_id = "@nothere:example.com"
        self.mod_user_id = "@mod:example.com"
        self.room_id = "!someroom:example.com"
        self.state = {
            (EventTypes.PowerLevels, ""): FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": {
                        "ban": 50,
                        "events": {
                            "m.room.avatar": 50,
                            "m.room.canonical_alias": 50,
                            "m.room.encryption": 100,
                            "m.room.history_visibility": 100,
                            "m.room.name": 50,
                            "m.room.power_levels": 100,
                            "m.room.server_acl": 100,
                            "m.room.tombstone": 100,
                        },
                        "events_default": 0,
                        "invite": 0,
                        "kick": 50,
                        "redact": 50,
                        "state_default": 50,
                        "users": {
                            self.user_id: 100,
                            self.left_user_id: 75,
                            self.mod_user_id: 50,
                        },
                        "users_default": 0
                    },
                    "room_id": self.room_id,
                },
                RoomVersions.V7,
            ),
            (EventTypes.JoinRules, ""): FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": EventTypes.JoinRules,
                    "state_key": "",
                    "content": {"join_rule": "public"},
                    "room_id": self.room_id,
                },
                RoomVersions.V7,
            ),
            (EventTypes.Member, self.mod_user_id): FrozenEventV3(
                {
                    "sender": self.mod_user_id,
                    "type": EventTypes.Member,
                    "state_key": self.mod_user_id,
                    "content": {"membership": Membership.JOIN},
                    "room_id": self.room_id,
                },
                RoomVersions.V7,
            ),
            (EventTypes.Member, self.left_user_id): FrozenEventV3(
                {
                    "sender": self.left_user_id,
                    "type": EventTypes.Member,
                    "state_key": self.left_user_id,
                    "content": {"membership": Membership.LEAVE},
                    "room_id": self.room_id,
                },
                RoomVersions.V7,
            ),
        }

    async def test_send_frozen_state(self):
        """Tests that the module allows frozen state change, and that users on the
        unfreeze blacklist are forbidden from unfreezing it.
        """
        module = create_module(
            config_override={"unfreeze_blacklist": ["evil.com"]},
        )

        # Test that an event with a valid value is allowed.
        freeze_event = self._build_frozen_event(sender=self.user_id, frozen=True)
        allowed, replacement = await module.check_event_allowed(freeze_event, self.state)
        self.assertTrue(allowed)
        # Make sure the replacement data we've got is the same event; we send it to force
        # Synapse to rebuild it because we've changed the state.
        self.assertEqual(replacement, freeze_event.get_dict())

        # Test that an unfreeze sent from a forbidden server isn't allowed.
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event(sender="@alice:evil.com", frozen=False),
            self.state,
        )
        self.assertFalse(allowed)

        # Test that a freeze sent from a forbidden server is allowed.
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event(sender="@alice:evil.com", frozen=True),
            self.state,
        )
        self.assertTrue(allowed)

        # Test that an event sent with an non-boolean value isn't allowed.
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event(sender=self.user_id, frozen="foo"),
            self.state,
        )
        self.assertFalse(allowed)

    async def test_power_levels_sent_when_freezing(self):
        """Tests that the module sends the right power levels update when it sees a room
        being unfrozen.
        """
        module = create_module()
        pl_event_dict = await self._send_frozen_event_and_get_pl_update(module, True)

        self.assertEqual(pl_event_dict["content"]["users_default"], 100)
        for user, pl in pl_event_dict["content"]["users"].items():
            self.assertEqual(pl, 100, user)

    async def test_power_levels_sent_when_unfreezing(self):
        """Tests that the module sends the right power levels update when it sees a room
        being unfrozen, and that the resulting power levels update is allowed when the
        room is frozen (since we persist it before finishing to process the unfreeze).
        """
        module = create_module()
        pl_event_dict = await self._send_frozen_event_and_get_pl_update(module, False)

        self.assertEqual(pl_event_dict["content"]["users_default"], 0)
        self.assertEqual(pl_event_dict["content"]["users"][self.user_id], 100)

        # Make sure the power level event is allowed when the room is frozen (since it
        # will be sent before the frozen event finishes persisting).
        self.state[(FROZEN_STATE_TYPE, "")] = self._build_frozen_event(self.user_id, True)
        pl_event = FrozenEventV3(pl_event_dict, RoomVersions.V7)
        allowed, replacement = await module.check_event_allowed(pl_event, self.state)
        self.assertTrue(allowed)
        self.assertIsNone(replacement)

    async def test_join_rules_sent_when_freezing(self):
        """Tests that the module resets the join rules to "invite" if the room gets
        frozen.
        """
        module = create_module()
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event(sender=self.user_id, frozen=True),
            self.state,
        )
        self.assertTrue(allowed)

        # Test that two events have been sent (the join rules change and the PL change)
        # and that one of them is the correct join rules change.
        self.assertTrue(module._api.create_and_send_event_into_room.called)
        args = module._api.create_and_send_event_into_room.call_args_list
        self.assertEqual(len(args), 2)
        join_rules_dict, _ = args[0]
        self.assertEqual(join_rules_dict[0]["content"]["join_rule"], "invite")

    async def test_cannot_send_messages_when_frozen(self):
        """Tests that users can't send messages when the room is frozen. Also tests that
        the power levels can't be updated in a different way than how it would happen
        with an unfreeze of the room.
        """
        self.state[(FROZEN_STATE_TYPE, "")] = self._build_frozen_event(self.user_id, True)
        module = create_module()

        # Test that a normal message event isn't allowed when the room is frozen.
        allowed, _ = await module.check_event_allowed(
            FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": EventTypes.Message,
                    "content": {"msgtype": "m.text", "body": "hello world"},
                    "room_id": self.room_id,
                },
                RoomVersions.V7,
            ),
            self.state,
        )
        self.assertFalse(allowed)

        # Check that, when the room is frozen, sending a PL update that sets the users
        # default back to 0 without naming a new admin isn't allowed.
        new_pl_event = copy.deepcopy(self.state[(EventTypes.PowerLevels, "")].get_dict())
        new_pl_event["content"]["users"] = {}
        new_pl_event["content"]["users_default"] = 0
        allowed, _ = await module.check_event_allowed(
            FrozenEventV3(new_pl_event, RoomVersions.V7),
            self.state,
        )
        self.assertFalse(allowed)

        # Check that, when the room is frozen, sending a PL update that explictly
        # prevents someone from unfreezing the room isn't allowed.
        new_pl_event = copy.deepcopy(self.state[(EventTypes.PowerLevels, "")].get_dict())
        new_pl_event["content"]["users"] = {}
        new_pl_event["content"]["users"]["@bob:example.com"] = 50
        allowed, _ = await module.check_event_allowed(
            FrozenEventV3(new_pl_event, RoomVersions.V7),
            self.state,
        )
        self.assertFalse(allowed)

        # Check that, when the room is frozen, sending a PL update that sets the users
        # default back to 0 while naming someone else admin isn't allowed.
        new_pl_event = copy.deepcopy(self.state[(EventTypes.PowerLevels, "")].get_dict())
        new_pl_event["content"]["users"] = {}
        new_pl_event["content"]["users"]["@bob:example.com"] = 100
        new_pl_event["content"]["users_default"] = 0
        allowed, _ = await module.check_event_allowed(
            FrozenEventV3(new_pl_event, RoomVersions.V7),
            self.state,
        )
        self.assertFalse(allowed)

    async def test_can_leave_room_when_frozen(self):
        """Tests that users can still leave a room when it's frozen."""
        self.state[(FROZEN_STATE_TYPE, "")] = self._build_frozen_event(self.user_id, True)
        module = create_module()

        # Test that leaving the room is allowed.
        allowed, replacement = await module.check_event_allowed(
            FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": EventTypes.Member,
                    "content": {"membership": Membership.LEAVE},
                    "room_id": self.room_id,
                    "state_key": self.user_id,
                },
                RoomVersions.V7,
            ),
            self.state,
        )
        self.assertTrue(allowed)
        self.assertIsNone(replacement)

        # Test that kicking a user is not allowed.
        allowed, _ = await module.check_event_allowed(
            FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": EventTypes.Member,
                    "content": {"membership": Membership.LEAVE},
                    "room_id": self.room_id,
                    "state_key": "@bob:example.com",
                },
                RoomVersions.V7,
            ),
            self.state,
        )
        self.assertFalse(allowed)

    async def test_auto_freeze_when_last_admin_leaves(self):
        """Tests that the module freezes the room when it sees its last admin leave."""
        module = create_module()

        leave_event = FrozenEventV3(
            {
                "sender": self.user_id,
                "type": EventTypes.Member,
                "content": {"membership": Membership.LEAVE},
                "room_id": self.room_id,
                "state_key": self.user_id,
            },
            RoomVersions.V7,
        )

        allowed, replacement = await module.check_event_allowed(leave_event, self.state)
        self.assertTrue(allowed)
        self.assertEqual(replacement, None)

        # Test that the leave triggered a freeze of the room.
        self.assertTrue(module._api.create_and_send_event_into_room.called)
        args, _ = module._api.create_and_send_event_into_room.call_args
        self.assertEqual(len(args), 1)

        expected_dict = self._build_frozen_event(self.user_id, True).get_dict()
        del expected_dict["unsigned"]
        del expected_dict["signatures"]

        self.assertEqual(args[0], expected_dict)

    async def test_promote_when_last_admin_leaves(self):
        """Tests that the module promotes whoever has the highest non-default PL to admin
        when the last admin leaves, if the config allows it.
        """
        # Set the config flag to allow promoting custom PLs before freezing the room.
        module = create_module(config_override={"promote_moderators": True})

        # Make the last admin leave.
        leave_event = FrozenEventV3(
            {
                "sender": self.user_id,
                "type": EventTypes.Member,
                "content": {"membership": Membership.LEAVE},
                "room_id": self.room_id,
                "state_key": self.user_id,
            },
            RoomVersions.V7,
        )

        # Check that we get the right result back from the callback.
        allowed, replacement = await module.check_event_allowed(leave_event, self.state)
        self.assertTrue(allowed)
        self.assertEqual(replacement, None)

        # Test that a new event was sent into the room.
        self.assertTrue(module._api.create_and_send_event_into_room.called)
        args, _ = module._api.create_and_send_event_into_room.call_args
        self.assertEqual(len(args), 1)

        # Test that:
        #   * the event is a power levels update
        #   * the user who is PL 75 but left the room didn't get promoted
        #   * the user who was PL 50 and is still in the room got promoted
        evt_dict: dict = args[0]
        self.assertEqual(evt_dict["type"], EventTypes.PowerLevels, evt_dict)
        self.assertIsNotNone(evt_dict.get("state_key"))
        self.assertEqual(evt_dict["content"]["users"][self.left_user_id], 75, evt_dict)
        self.assertEqual(evt_dict["content"]["users"][self.mod_user_id], 100, evt_dict)

        # Now we push both the leave event and the power levels update into the state of
        # the room.
        self.state[(EventTypes.Member, self.user_id)] = leave_event
        self.state[(EventTypes.PowerLevels, "")] = FrozenEventV3(
            evt_dict, RoomVersions.V7,
        )

        # Make the mod (newly admin) leave the room.
        new_leave_event = FrozenEventV3(
            {
                "sender": self.mod_user_id,
                "type": EventTypes.Member,
                "content": {"membership": Membership.LEAVE},
                "room_id": self.room_id,
                "state_key": self.mod_user_id,
            },
            RoomVersions.V7,
        )

        # Check that we get the right result back from the callback.
        allowed, replacement = await module.check_event_allowed(
            new_leave_event, self.state,
        )
        self.assertTrue(allowed)
        self.assertEqual(replacement, None)

        # Test that a new event was sent into the room.
        self.assertTrue(module._api.create_and_send_event_into_room.called)
        args, _ = module._api.create_and_send_event_into_room.call_args
        self.assertEqual(len(args), 1)

        # Test that now that there's no user to promote anymore, the room gets frozen.
        expected_dict = self._build_frozen_event(self.mod_user_id, True).get_dict()
        del expected_dict["unsigned"]
        del expected_dict["signatures"]

        self.assertEqual(args[0], expected_dict)

    async def _send_frozen_event_and_get_pl_update(
        self, module: FreezeRoom, frozen: bool,
    ) -> dict:
        """Sends a frozen state change and get the dict for the power level update it
        triggered.
        """
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event(sender=self.user_id, frozen=frozen),
            self.state,
        )
        self.assertTrue(allowed)

        self.assertTrue(module._api.create_and_send_event_into_room.called)
        args, _ = module._api.create_and_send_event_into_room.call_args
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0]["type"], EventTypes.PowerLevels)

        return args[0]

    def _build_frozen_event(self, sender: str, frozen: bool) -> FrozenEventV3:
        """Build a new org.matrix.room.frozen event with the given sender and value."""
        event_dict = {
            "sender": sender,
            "type": FROZEN_STATE_TYPE,
            "content": {"frozen": frozen},
            "room_id": self.room_id,
            "state_key": "",
        }

        return FrozenEventV3(event_dict, RoomVersions.V7)