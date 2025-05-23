from __future__ import annotations

import abc
import contextlib
import dataclasses
import itertools
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Type

from action_log import Action, Actionlog, LogEntry
from adjacency import board_layout, dijkstra, gen_144p


@dataclasses.dataclass
class Pieces:
    cnt: int
    tipe: PieceType

    def __str__(self) -> str:
        return str(self.cnt)


LAIR_KEY = "LAIR"


class Land:
    def __init__(
        self,
        key: str,  # example: 🌙R4
        display_name: str,  # example: 🌙R4 OR 🌙R4:Wetlands:
        land_type: str,  # example: M
        explorers: int,
        towns: int,
        cities: int,
        dahan: int,
        conf: LairConf,
    ):
        self.key = key
        self.display_name = display_name
        self.land_type = land_type
        self.explorers = Explorer.new(explorers)
        self.towns = Town.new(towns)
        self.cities = City.new(cities)
        self.dahan = Dahan.new(dahan)
        self.mr_explorers = Explorer.new()
        self.mr_towns = Town.new()
        self.conf = conf

    def mr(self) -> None:
        for tipe in (Explorer, Town, City):
            mr = tipe.select_mr(self)
            tipe.select(self).cnt += mr.cnt
            mr.cnt = 0

    def add_pieces(
        self,
        explorers: int,
        towns: int,
        cities: int,
        dahan: int,
        allow_negative: bool = False,
    ) -> None:
        for piece, added in zip(
            (self.explorers, self.towns, self.cities, self.dahan),
            (explorers, towns, cities, dahan),
        ):
            piece.cnt += added
            assert allow_negative or piece.cnt >= 0, f"land {self.key}"

    def total_invaders(self) -> int:
        return self.explorers.cnt + self.towns.cnt + self.cities.cnt

    def __str__(self) -> str:
        pieces = ", ".join(
            [
                f"{tipe.name(self.conf.piece_names)}={tipe.select(self).cnt}"
                for tipe in (
                    Explorer,
                    Town,
                    City,
                    Dahan,
                )
            ]
        )
        return f"({pieces})"


@dataclasses.dataclass
class PieceNames:
    explorer: str
    town: str
    city: str
    dahan: str


class _PieceType(abc.ABC):
    @classmethod
    def new(cls, cnt: int = 0) -> Pieces:
        return Pieces(cnt=cnt, tipe=cls)

    health: int
    fear: int
    response: Optional[PieceType]

    @classmethod
    @abc.abstractmethod
    def select(cls, land: Land) -> Pieces:
        pass

    @classmethod
    @abc.abstractmethod
    def select_mr(cls, land: Land) -> Pieces:
        pass

    @classmethod
    @abc.abstractmethod
    def name(cls, pn: PieceNames) -> str:
        pass


PieceType = Type[_PieceType]


class Void(_PieceType):
    health = 0
    fear = 0
    response = None

    @classmethod
    def select(cls, land: Land) -> Pieces:
        return cls.new()

    @classmethod
    def select_mr(cls, land: Land) -> Pieces:
        return cls.new()

    @classmethod
    def name(cls, pn: PieceNames) -> str:
        return "void"


class Explorer(_PieceType):
    health = 1
    fear = 0
    response = None

    @classmethod
    def select(cls, land: Land) -> Pieces:
        return land.explorers

    @classmethod
    def select_mr(cls, land: Land) -> Pieces:
        return land.mr_explorers

    @classmethod
    def name(cls, pn: PieceNames) -> str:
        return pn.explorer


class Town(_PieceType):
    health = 2
    fear = 1
    response = Explorer

    @classmethod
    def select(cls, land: Land) -> Pieces:
        return land.towns

    @classmethod
    def select_mr(cls, land: Land) -> Pieces:
        return land.mr_towns

    @classmethod
    def name(cls, pn: PieceNames) -> str:
        return pn.town


class City(_PieceType):
    health = 3
    fear = 2
    response = Town

    @classmethod
    def select(cls, land: Land) -> Pieces:
        return land.cities

    @classmethod
    def select_mr(cls, land: Land) -> Pieces:
        return Void.new()

    @classmethod
    def name(cls, pn: PieceNames) -> str:
        return pn.city


class Dahan(_PieceType):
    health = 2
    fear = 0
    response = None

    @classmethod
    def select(cls, land: Land) -> Pieces:
        return land.dahan

    @classmethod
    def select_mr(cls, land: Land) -> Pieces:
        return Void.new()

    @classmethod
    def name(cls, pn: PieceNames) -> str:
        return pn.dahan


@dataclasses.dataclass
class LairConf:
    land_priority: str
    reserve_gathers_blue: int
    reserve_gathers_orange: int
    reckless_offensive: List[str]
    piece_names: PieceNames
    show_range: bool

    def _land_type_priority(self, land_type: str) -> int:
        try:
            return self.land_priority.index(land_type)
        except ValueError:
            return len(self.land_priority)

    def land_type_priority(self, land_type: str, coastal: bool) -> int:
        priority = self._land_type_priority(land_type)
        if coastal:
            priority = min(priority, self._land_type_priority("C"))
        return priority


ConvertLand = Callable[[Land], Land]


@dataclasses.dataclass
class LairState:
    r0: Land
    r1: List[Land]
    r2: List[Land]
    ignored: List[Land]
    log: Actionlog
    dist: Dict[str, int]
    total_gathers: int = 0
    wasted_damage: int = 0
    wasted_downgrades: int = 0
    wasted_invader_gathers: int = 0
    wasted_dahan_gathers: int = 0
    fear: int = 0


def construct_distance_map(
    conf: LairConf,
    lands: Dict[str, Land],
    map: gen_144p.Map144P,
    src: str,
) -> Tuple[Dict[str, int], Dict[str, str]]:
    def tiebreaker(
        land: board_layout.Land,
        dist: Dict[str, int],
        prev: Dict[str, str],
    ) -> dijkstra.Comparable:
        priority = conf.land_type_priority(land.terrain.value, land.coastal)
        key = land.key
        while dist[key] > 1 and key in prev:
            key = prev[key]
        if dist[key] == 1:
            r1_dahan = lands[key].dahan.cnt
        else:
            r1_dahan = 0  # it's the lair..

        prev_land = land.key
        ignored = False
        while prev_land != src:
            if prev_land not in lands:
                ignored = True
                break
            prev_land = prev[prev_land]

        return (ignored, -priority, r1_dahan)

    return dijkstra.distances_from(map.land(src), tiebreaker)


class Lair:
    def __init__(
        self,
        lands: Dict[str, Land],
        ignored: List[Land],
        src: str,
        conf: LairConf,
        log: Actionlog,
        map: gen_144p.Map144P,
    ):
        self.map = map
        self.conf = conf
        self.uncommitted: List[LogEntry] = []

        dist, prev = construct_distance_map(conf, lands, map, src)
        self.gathers_to = {
            key: lands.get(prev[key])
            for key in lands.keys()
            if key in prev and dist[key] != 0
        }
        r0 = lands[LAIR_KEY]
        r1 = []
        r2 = []
        for key, land in lands.items():
            if key == LAIR_KEY or key not in prev or dist[key] == 0:
                continue
            if conf.show_range:
                land.display_name += f" ({dist[key]})"
            if dist[key] == 1:
                self.gathers_to[key] = r0
                r1.append(land)
            elif self._r1_gathers_to(land, dist) is not None:
                r2.append(land)
            else:
                ignored.append(land)

        self.state = LairState(
            r0=r0,
            r1=r1,
            r2=r2,
            ignored=ignored,
            log=log,
            dist=dist,
        )

    def _r1_gathers_to(
        self,
        land: Land,
        dist: Dict[str, int],
    ) -> Optional[Land]:
        while dist[land.key] > 1:
            prev_land = self.gathers_to[land.key]
            if prev_land is None:
                return None
            land = prev_land
        return land

    def _commit_log(self) -> None:
        self.uncommitted.sort(key=lambda entry: entry.src_land or "")
        for entry in self.uncommitted:
            self.state.log.entry(entry)
        self.uncommitted = []

    def _noncommit_entry(self, entry: LogEntry) -> None:
        self.uncommitted.append(entry)

    def _xchg(
        self,
        src_land: Land,
        src_tipe: PieceType,
        tgt: Pieces,
        cnt: int,
    ) -> int:
        if any(
            land_key in src_land.key for land_key in self.conf.reckless_offensive
        ) and src_tipe in (Town, Dahan):
            leave = 2
        else:
            leave = 0
        src = src_tipe.select(src_land)
        actual = min(max(src.cnt - leave, 0), cnt)
        src.cnt -= actual
        tgt.cnt += actual
        return actual

    def _gather(self, tipe: PieceType, land: Land, cnt: int) -> int:
        gathers_to = self.gathers_to[land.key]
        assert gathers_to
        actual = self._xchg(land, tipe, tipe.select(gathers_to), cnt)
        self.state.total_gathers += actual
        if actual:
            piece_name = tipe.name(self.conf.piece_names)
            self._noncommit_entry(
                LogEntry(
                    action=Action.GATHER,
                    src_land=land.display_name,
                    src_piece=piece_name,
                    tgt_land=gathers_to.display_name,
                    tgt_piece=piece_name,
                    count=actual,
                )
            )
        return actual

    def _downgrade(self, tipe: PieceType, land: Land, cnt: int) -> int:
        assert tipe.response
        actual = self._xchg(land, tipe, tipe.response.select(land), cnt)
        if actual:
            self._noncommit_entry(
                LogEntry(
                    action=Action.DOWNGRADE,
                    src_land=land.display_name,
                    src_piece=tipe.name(self.conf.piece_names),
                    tgt_land=land.display_name,
                    tgt_piece=tipe.response.name(self.conf.piece_names),
                    count=actual,
                )
            )
        return actual

    def _lair1(self) -> None:
        r0 = self.state.r0
        downgrades = (r0.explorers.cnt + r0.dahan.cnt) // 3
        self.state.log.entry(LogEntry(text=f"available downgrades: {downgrades}"))
        downgrades -= self._downgrade(Town, r0, downgrades)
        downgrades -= self._downgrade(City, r0, downgrades)
        self.state.wasted_downgrades += downgrades

    def _r1_least_dahan(self) -> List[Land]:
        return sorted(self.state.r1, key=lambda land: land.dahan.cnt)

    def _r1_most_dahan(self) -> List[Land]:
        return sorted(self.state.r1, key=lambda land: -land.dahan.cnt)

    def _lair2(self) -> None:
        gathers = 1
        for tipe in (Explorer, Town):
            for land in self._r1_most_dahan():
                gathers -= self._gather(tipe, land, gathers)
        self.state.wasted_invader_gathers += gathers

        gathers = 1
        for land in self._r1_most_dahan():
            gathers -= self._gather(Dahan, land, gathers)
        self.state.wasted_dahan_gathers += gathers

    def _reserve(self, reserve: int, what: str, cnt: int) -> int:
        if reserve:
            to_reserve = min(cnt, reserve)
            self.state.log.entry(LogEntry(text=f"reserved {to_reserve} {what}"))
            return to_reserve
        return 0

    def _least_r1_dahan_land_priority_key(
        self,
        land: Land,
    ) -> Tuple[int, int, int]:
        try:
            coastal = self.map.land(land.key).coastal
        except KeyError:
            coastal = False
        land_priority = self.conf.land_type_priority(land.land_type, coastal)

        dist = self.state.dist[land.key]
        r1_land = self._r1_gathers_to(land, self.state.dist)
        assert r1_land

        return (land_priority, dist, r1_land.dahan.cnt)

    def _lair3(self, reserve: int) -> None:
        r0 = self.state.r0
        gathers = (r0.explorers.cnt + r0.dahan.cnt) // 6
        self.state.log.entry(LogEntry(text=f"available gathers: {gathers}"))
        with self.state.log.indent():
            gathers -= self._reserve(reserve, "gathers", gathers)

        for land in sorted(
            self.state.r2,
            key=self._least_r1_dahan_land_priority_key,
        ):
            gathers -= self._gather(Town, land, gathers)
            gathers -= self._gather(City, land, gathers)
            gathers -= self._gather(Explorer, land, gathers)

        # TODO: loop again if we have gathers left and didn't clear r2,
        #       but need to ensure resulting log doesn't break causality.

        for tipe in (Explorer, Town, City):
            for land in self._r1_most_dahan():
                gathers -= self._gather(tipe, land, gathers)

        self._commit_log()
        self.state.log.entry(
            LogEntry(text=f"unused gathers left at end of slurp: {gathers}")
        )
        self.state.wasted_invader_gathers += gathers

    @contextlib.contextmanager
    def _top_log(self, what: str) -> Iterator[None]:
        oldlog = self.state.log
        with self.state.log.fork() as newlog:
            self.state.log = newlog
            before = str(self.state.r0)
            yield
            self._commit_log()
            oldlog.entry(
                LogEntry(
                    text=f"{what} in {self.state.r0.key}: {before} => {self.state.r0}"
                )
            )
        self.state.log = oldlog

    def _lair_all(self, colour: str, reserve: int) -> None:
        with self._top_log(f"lair-{colour}-thresh1"):
            self._lair1()
        with self._top_log(f"lair-{colour}-thresh2"):
            self._lair2()
        with self._top_log(f"lair-{colour}-thresh3"):
            self._lair3(reserve)

    def lair_blue(self) -> None:
        self._lair_all("blue", self.conf.reserve_gathers_blue)

    def lair_orange(self) -> None:
        self._lair_all("orange", self.conf.reserve_gathers_orange)

    def _call_one(
        self,
        it: Callable[[], List[Land]],
        tipe: PieceType,
        gathers: int,
    ) -> int:
        for land in it():
            gathers -= self._gather(tipe, land, gathers)
        return gathers

    def call(self) -> None:
        with self._top_log("call"):
            self.state.wasted_invader_gathers += self._call_one(
                self._r1_most_dahan, Town, 5
            )
            self.state.wasted_invader_gathers += self._call_one(
                self._r1_most_dahan, Explorer, 15
            )
            self.state.wasted_dahan_gathers += self._call_one(
                self._r1_least_dahan, Dahan, 5
            )

    def _damage(self, land: Land, tipe: PieceType, dmg: int) -> int:
        assert land.key in self.gathers_to

        respond_to: Optional[Land] = None
        if tipe.response:
            if land.dahan.cnt:
                respond_to = land
            else:
                respond_to = self.gathers_to[land.key]
            assert respond_to
            response = tipe.response.select_mr(respond_to)
        else:
            response = Void.new()

        kill = self._xchg(land, tipe, response, dmg // tipe.health)
        if kill:
            self._noncommit_entry(
                LogEntry(
                    action=Action.DESTROY,
                    src_land=land.display_name,
                    src_piece=tipe.name(self.conf.piece_names),
                    tgt_land=respond_to.display_name if respond_to else "",
                    tgt_piece=(
                        tipe.response.name(self.conf.piece_names)
                        if tipe.response
                        else ""
                    ),
                    count=kill,
                )
            )
        self.state.fear += kill * tipe.fear
        return kill * tipe.health

    def _ravage(self) -> None:
        r0 = self.state.r0
        dmg = max(0, r0.explorers.cnt - 6) + r0.towns.cnt * 2 + r0.cities.cnt * 3

        lands = sorted(
            self.state.r1,
            key=self._least_r1_dahan_land_priority_key,
        )
        for land in lands:
            dmg -= self._damage(land, Town, dmg)
            dmg -= self._damage(land, City, dmg)
        for land in lands:
            dmg -= self._damage(land, Explorer, dmg)

        self._commit_log()
        self.state.log.entry(
            LogEntry(text=f"unused damage left at end of ravage: {dmg}")
        )
        self.state.wasted_damage += dmg

        for land in itertools.chain([self.state.r0], self.state.r1):
            land.mr()

    def ravage(self) -> None:
        with self._top_log("ravage"):
            self._ravage()

    def _add(self, land: Land, tipe: PieceType, cnt: int) -> None:
        tipe.select(land).cnt += cnt
        self.state.log.entry(
            LogEntry(
                action=Action.ADD,
                tgt_land=land.display_name,
                tgt_piece=tipe.name(self.conf.piece_names),
                count=cnt,
            )
        )

    def _build(self, land: Land) -> None:
        if all(tipe.select(land).cnt == 0 for tipe in (Explorer, Town, City)):
            return

        tipe: PieceType
        if land.towns.cnt > land.cities.cnt:
            tipe = City
        else:
            tipe = Town
        self._add(land, tipe, 1)

    def blur(self) -> None:
        with self._top_log("blur"):
            if self.state.r0.dahan.cnt > 0:
                self._add(self.state.r0, Dahan, 1)
            self._build(self.state.r0)
            self._ravage()

    def blur2(self) -> None:
        self.blur()
        self.blur()
