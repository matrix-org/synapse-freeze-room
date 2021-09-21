# Freeze room

This module uses third-party rules callbacks from Synapse's module interface to identify
when the last admin of a room leaves it, and when they do it "freezes" the room.

Freezing the room means adding a custom `org.matrix.room.frozen` to the state of the room.
If the content of this event says the room is frozen (`{"frozen": true}`), then the module
will prevent any event to be sent in the room. The only exceptions are letting users leave
the room, and letting a user unfreeze it and taking over the room's administration.

Unfreezing the room means sending an `org.matrix.room.frozen` state event into the room
with the content `{"frozen": false}`. The user unfreezing the room will then automatically
become the only administrator in the room.

As with other modules using third-party rules callbacks, it is recommended that this
module is only used in a closed federation in which every server has this module
configured the same way.

This module requires Synapse v1.39.0 or later.

## Installation

This plugin can be installed via PyPI:

```
pip install synapse-freeze-room
```

## Config

Add the following to your Synapse config:

```yaml
modules:
  - module: freeze_room.FreezeRoom
    config:
      # Optional: a list of servers that are forbidden from unfreezing rooms.
      unfreeze_blacklist:
        - evil.com
        - foo.com
      # Optional: if set to true, when the last admin in a room leaves it, the module will
      # try to promote any moderator (or user with the highest power level) as admin. In
      # this mode, it will only freeze the room if it can't find any user to promote.
      # Defaults to false.
      promote_moderators: false
```

## Development and Testing

This repository uses `tox` to run tests.

### Tests

This repository uses `unittest` to run the tests located in the `tests`
directory. They can be ran with `tox -e tests`.

### Making a release

```
git tag vX.Y
python3 setup.py sdist
twine upload dist/synapse-freeze-room-X.Y.tar.gz
git push origin vX.Y
```