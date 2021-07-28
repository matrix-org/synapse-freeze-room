# From Python 3.8 onwards, aiounittest.AsyncTestCase can be replaced by
# unittest.IsolatedAsyncioTestCase, so we'll be able to get rid of this dependency when
# we stop supporting Python < 3.8 in Synapse.
import aiounittest
from synapse.api.room_versions import RoomVersions
from synapse.events import FrozenEventV3

from freeze_room import FROZEN_STATE_TYPE, EventTypes
from tests import create_module


class RoomFreezeTest(aiounittest.AsyncTestCase):
    def setUp(self):
        self.user_id = "@alice:example.com"
        self.state = {
            (EventTypes.PowerLevels, ""): FrozenEventV3(
                {
                    "sender": self.user_id,
                    "type": FROZEN_STATE_TYPE,
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
                            "@mod:example.com": 50,
                        },
                        "users_default": 0
                    },
                    "room_id": "!someroom:example.com",
                },
                RoomVersions.V7,
            )
        }

    async def test_send_frozen_state(self):
        module = create_module(
            config_override={"unfreeze_blacklist": ["evil.com"]},
        )

        # Test that an event with a valid value is allowed.
        # TODO: Check that a PL event was sent and it's the right one
        allowed, replacement = await module.check_event_allowed(
            self._build_frozen_event(sender=self.user_id, frozen=True),
            self.state,
        )
        self.assertTrue(allowed)
        # Make sure the replacement data we've got is the same event; we send it to force
        # Synapse to rebuild it because we've changed the state.
        self.assertEqual(replacement["sender"], self.user_id)
        self.assertEqual(replacement["type"], FROZEN_STATE_TYPE)
        self.assertEqual(replacement["content"]["frozen"], True)

        # Test that an event sent from a forbidden server isn't allowed.
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event("@alice:evil.com", True),
            self.state,
        )
        self.assertFalse(allowed)

        # Test that an event sent with an non-boolean value isn't allowed.
        allowed, _ = await module.check_event_allowed(
            self._build_frozen_event("@alice:evil.com", "foo"),
            self.state,
        )
        self.assertFalse(allowed)

    async def test_power_levels_sent_when_freezing(self):
        pl_event_dict = await self._send_frozen_event_and_get_power_levels_update(True)

        self.assertEqual(pl_event_dict["content"]["users_default"], 100)
        for user, pl in pl_event_dict["content"]["users"].items():
            self.assertEqual(pl, 100, user)

    async def test_power_levels_sent_when_unfreezing(self):
        pl_event_dict = await self._send_frozen_event_and_get_power_levels_update(False)

        self.assertEqual(pl_event_dict["content"]["users_default"], 0)
        self.assertEqual(pl_event_dict["content"]["users"][self.user_id], 100)

    async def _send_frozen_event_and_get_power_levels_update(self, frozen: bool) -> dict:
        module = create_module()
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
        event_dict = {
            "sender": sender,
            "type": FROZEN_STATE_TYPE,
            "content": {"frozen": frozen},
            "room_id": "!someroom:example.com",
        }

        return FrozenEventV3(event_dict, RoomVersions.V7)