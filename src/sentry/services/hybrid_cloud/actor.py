# Please do not use
#     from __future__ import annotations
# in modules such as this one where hybrid cloud data models or service classes are
# defined, because we want to reflect on type annotations and avoid forward references.

from collections import defaultdict
from collections.abc import Iterable, MutableMapping
from enum import Enum
from typing import TYPE_CHECKING, Any, Union

from sentry.services.hybrid_cloud import RpcModel
from sentry.services.hybrid_cloud.organization import RpcTeam
from sentry.services.hybrid_cloud.user import RpcUser
from sentry.services.hybrid_cloud.user.service import user_service

if TYPE_CHECKING:
    from sentry.models.actor import Actor
    from sentry.models.team import Team
    from sentry.models.user import User


class ActorType(str, Enum):
    USER = "User"
    TEAM = "Team"


ActorTarget = Union["Actor", "RpcActor", "User", "RpcUser", "Team", "RpcTeam"]


class RpcActor(RpcModel):
    """Can represent any model object with a foreign key to Actor."""

    id: int
    """The id of the user/team this actor represents"""

    actor_type: ActorType
    """Whether this actor is a User or Team"""

    slug: str | None = None

    def __post_init__(self) -> None:
        if (self.actor_type == ActorType.TEAM) == (self.slug is None):
            raise ValueError("Slugs are expected for teams only")

    def __hash__(self) -> int:
        return hash((self.id, self.actor_type))

    @classmethod
    def resolve_many(cls, actors: Iterable["RpcActor"]) -> list["Team | RpcUser"]:
        """
        Resolve a list of actors in a batch to the Team/User the Actor references.

        Will generate more efficient queries to load actors than calling
        RpcActor.resolve() individually will.
        """
        from sentry.models.team import Team

        if not actors:
            return []
        actors_by_type: dict[ActorType, list[RpcActor]] = defaultdict(list)
        for actor in actors:
            actors_by_type[actor.actor_type].append(actor)
        results: dict[tuple[ActorType, int], Team | RpcUser] = {}
        for actor_type, actor_list in actors_by_type.items():
            if actor_type == ActorType.USER:
                for user in user_service.get_many(filter={"user_ids": [u.id for u in actor_list]}):
                    results[(actor_type, user.id)] = user
            if actor_type == ActorType.TEAM:
                for team in Team.objects.filter(id__in=[t.id for t in actor_list]):
                    results[(actor_type, team.id)] = team

        return list(filter(None, [results.get((actor.actor_type, actor.id)) for actor in actors]))

    @classmethod
    def many_from_object(cls, objects: Iterable[ActorTarget]) -> list["RpcActor"]:
        """
        Create a list of RpcActor instances based on a collection of 'objects'

        Objects will be grouped by the kind of actor they would be related to.
        Queries for actors are batched to increase efficiency. Users that are
        missing actors will have actors generated.
        """
        from sentry.models.team import Team
        from sentry.models.user import User

        result: list["RpcActor"] = []
        grouped_by_type: MutableMapping[str, list[int]] = defaultdict(list)
        team_slugs: MutableMapping[int, str] = {}
        for obj in objects:
            if isinstance(obj, cls):
                result.append(obj)
            if isinstance(obj, (User, RpcUser)):
                grouped_by_type[ActorType.USER].append(obj.id)
            if isinstance(obj, (Team, RpcTeam)):
                team_slugs[obj.id] = obj.slug
                grouped_by_type[ActorType.TEAM].append(obj.id)

        if grouped_by_type[ActorType.TEAM]:
            team_ids = grouped_by_type[ActorType.TEAM]
            for team_id in team_ids:
                result.append(
                    RpcActor(
                        id=team_id,
                        actor_type=ActorType.TEAM,
                        slug=team_slugs.get(team_id),
                    )
                )

        if grouped_by_type[ActorType.USER]:
            user_ids = grouped_by_type[ActorType.USER]
            for user_id in user_ids:
                result.append(RpcActor(id=user_id, actor_type=ActorType.USER))
        return result

    @classmethod
    def from_object(cls, obj: ActorTarget) -> "RpcActor":
        """
        fetch_actor: whether to make an extra query or call to fetch the actor id
                     Without the actor_id the RpcActor acts as a tuple of id and type.
        """
        from sentry.models.team import Team
        from sentry.models.user import User

        if isinstance(obj, cls):
            return obj
        if isinstance(obj, User):
            return cls.from_orm_user(obj)
        if isinstance(obj, Team):
            return cls.from_orm_team(obj)
        if isinstance(obj, RpcUser):
            return cls.from_rpc_user(obj)
        if isinstance(obj, RpcTeam):
            return cls.from_rpc_team(obj)
        raise TypeError(f"Cannot build RpcActor from {type(obj)}")

    @classmethod
    def from_orm_user(cls, user: "User") -> "RpcActor":
        return cls(
            id=user.id,
            actor_type=ActorType.USER,
        )

    @classmethod
    def from_rpc_user(cls, user: RpcUser) -> "RpcActor":
        return cls(
            id=user.id,
            actor_type=ActorType.USER,
        )

    @classmethod
    def from_orm_team(cls, team: "Team") -> "RpcActor":
        return cls(id=team.id, actor_type=ActorType.TEAM, slug=team.slug)

    @classmethod
    def from_rpc_team(cls, team: RpcTeam) -> "RpcActor":
        return cls(id=team.id, actor_type=ActorType.TEAM, slug=team.slug)

    @classmethod
    def from_identifier(cls, id: str | int | None) -> "RpcActor | None":
        """
        Parse an actor identifier into an RpcActor

        Forms `id` can take:
            1231 -> look up User by id
            "1231" -> look up User by id
            "user:1231" -> look up User by id
            "team:1231" -> look up Team by id
            "maiseythedog" -> look up User by username
            "maisey@dogsrule.com" -> look up User by primary email
        """
        if not id:
            return None
        # If we have an integer, fall back to assuming it's a User
        if isinstance(id, int):
            return cls(id=id, actor_type=ActorType.USER)

        # If the actor_identifier is a simple integer as a string,
        # we're also a User
        if id.isdigit():
            return cls(id=int(id), actor_type=ActorType.USER)

        if id.startswith("user:"):
            return cls(id=int(id[5:]), actor_type=ActorType.USER)

        if id.startswith("team:"):
            return cls(id=int(id[5:]), actor_type=ActorType.TEAM)

        try:
            user = user_service.get_by_username(username=id)[0]
            return cls(id=user.id, actor_type=ActorType.USER)
        except IndexError as e:
            raise ValueError(f"Unable to resolve actor identifier: {e}")

    @classmethod
    def from_id(cls, user_id: int | None = None, team_id: int | None = None) -> "RpcActor":
        if user_id and team_id:
            raise ValueError("You can only provide one of user_id and team_id")
        if user_id:
            return cls(id=user_id, actor_type=ActorType.USER)
        if team_id:
            return cls(id=team_id, actor_type=ActorType.TEAM)
        raise ValueError("You must provide one of user_id and team_id")

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, self.__class__)
            and self.id == other.id
            and self.actor_type == other.actor_type
        )

    def resolve(self) -> Union["Team", "RpcUser"] | None:
        from sentry.models.team import Team

        if self.actor_type == ActorType.TEAM:
            return Team.objects.filter(id=self.id).first()
        if self.actor_type == ActorType.USER:
            return user_service.get_user(user_id=self.id)

    @property
    def identifier(self) -> str:
        return f"{self.actor_type.lower()}:{self.id}"
