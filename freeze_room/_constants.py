class Membership:

    """Represents the membership states of a user in a room."""

    INVITE = "invite"
    JOIN = "join"
    KNOCK = "knock"
    LEAVE = "leave"
    BAN = "ban"
    LIST = (INVITE, JOIN, KNOCK, LEAVE, BAN)


class EventTypes:
    Member = "m.room.member"
    Create = "m.room.create"
    Tombstone = "m.room.tombstone"
    JoinRules = "m.room.join_rules"
    PowerLevels = "m.room.power_levels"
    Aliases = "m.room.aliases"
    Redaction = "m.room.redaction"
    ThirdPartyInvite = "m.room.third_party_invite"
    RelatedGroups = "m.room.related_groups"

    RoomHistoryVisibility = "m.room.history_visibility"
    CanonicalAlias = "m.room.canonical_alias"
    Encrypted = "m.room.encrypted"
    RoomAvatar = "m.room.avatar"
    RoomEncryption = "m.room.encryption"
    GuestAccess = "m.room.guest_access"

    # These are used for validation
    Message = "m.room.message"
    Topic = "m.room.topic"
    Name = "m.room.name"

    ServerACL = "m.room.server_acl"
    Pinned = "m.room.pinned_events"

    Retention = "m.room.retention"

    Dummy = "org.matrix.dummy_event"

    SpaceChild = "m.space.child"
    SpaceParent = "m.space.parent"
