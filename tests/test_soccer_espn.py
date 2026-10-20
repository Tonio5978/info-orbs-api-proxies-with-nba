"""Tests of the ESPN -> API projection on recorded ESPN responses (tests/fixtures/soccer)."""
import copy
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import soccer_espn as espn

FIXTURES = Path(__file__).parent / "fixtures" / "soccer"
CONFIG = Path(__file__).parent.parent / "config"
PARIS = ZoneInfo("Europe/Paris")
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class ProjectEventTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load("scoreboard_mixed.json")["events"]
        cls.events = [espn.project_event(e) for e in cls.raw]
        cls.by_name = {e["status"]["type"]["name"]: e for e in cls.events}

    def test_fixture_covers_every_case(self):
        self.assertTrue({"STATUS_SCHEDULED", "STATUS_HALFTIME", "STATUS_SECOND_HALF", "STATUS_FULL_TIME",
                         "STATUS_ABANDONED"} <= set(self.by_name))

    def test_exact_structure(self):
        for e in self.events:
            self.assertEqual(list(e), ["id", "uid", "date", "name", "status", "venue", "competitions"])
            self.assertEqual(list(e["status"]["type"]), ["state", "name", "completed"])
            self.assertEqual(list(e["venue"]), ["displayName"])
            self.assertEqual(len(e["competitions"]), 1)
            self.assertEqual(list(e["competitions"][0]), ["altGameNote", "competitors", "details"])
            for c in e["competitions"][0]["competitors"]:
                self.assertEqual(list(c), ["homeAway", "score", "team"])
                self.assertEqual(list(c["team"]), ["id", "displayName", "abbreviation", "color", "alternateColor"])

    def test_values_are_espn_values(self):
        for raw, e in zip(self.raw, self.events):
            for k in ("id", "uid", "date", "name"):
                self.assertEqual(e[k], raw[k])
            self.assertEqual(e["status"]["type"]["name"], raw["status"]["type"]["name"])  # Never renamed
            self.assertEqual(e["status"]["displayClock"], raw["status"]["displayClock"])
            self.assertEqual(e["venue"]["displayName"], raw["venue"]["displayName"])
            self.assertEqual(e["competitions"][0]["altGameNote"], raw["competitions"][0]["altGameNote"])

    def test_period_absent_when_scheduled(self):
        scheduled = self.by_name["STATUS_SCHEDULED"]
        self.assertEqual(scheduled["status"]["type"]["state"], "pre")
        self.assertNotIn("period", scheduled["status"])
        for name in ("STATUS_HALFTIME", "STATUS_SECOND_HALF", "STATUS_FULL_TIME"):
            self.assertIsInstance(self.by_name[name]["status"]["period"], int, name)

    def test_period_absent_when_scheduled_even_if_espn_sends_one(self):
        raw = copy.deepcopy(next(e for e in self.raw if e["status"]["type"]["state"] == "pre"))
        raw["status"]["period"] = 0
        self.assertNotIn("period", espn.project_event(raw)["status"])

    def test_two_competitors_colors_without_hash_scores_as_strings(self):
        for e in self.events:
            competitors = e["competitions"][0]["competitors"]
            self.assertEqual(sorted(c["homeAway"] for c in competitors), ["away", "home"])
            for c in competitors:
                self.assertIsInstance(c["score"], str)
                self.assertRegex(c["team"]["color"], HEX)
                self.assertRegex(c["team"]["alternateColor"], HEX)

    def test_hash_is_stripped_and_numeric_score_stringified(self):
        raw = copy.deepcopy(self.raw[0])
        comp = raw["competitions"][0]["competitors"][0]
        comp["team"]["color"], comp["team"]["alternateColor"], comp["score"] = "#AB12CD", "#ffffff", 3
        projected = espn.project_event(raw)["competitions"][0]["competitors"][0]
        self.assertEqual((projected["team"]["color"], projected["team"]["alternateColor"], projected["score"]),
                         ("AB12CD", "ffffff", "3"))

    def test_details(self):
        details = [d for e in self.events for d in e["competitions"][0]["details"]]
        self.assertTrue(details)
        types = {d["type"]["text"] for d in details}
        self.assertTrue({"Penalty - Scored", "Own Goal", "Red Card"} <= types, types)
        allowed = {"type", "scoringPlay", "penaltyKick", "ownGoal", "team", "clock", "athletesInvolved", "corrected", "score"}
        for d in details:
            self.assertLessEqual(set(d), allowed)
            self.assertEqual(list(d["type"]), ["text"])
            self.assertEqual(list(d["team"]), ["id"])
            self.assertEqual(list(d["clock"]), ["displayValue"])
            for flag in ("scoringPlay", "penaltyKick", "ownGoal"):
                self.assertIsInstance(d[flag], bool)
            for athlete in d["athletesInvolved"]:
                self.assertEqual(list(athlete), ["shortName", "jersey"])
        penalty = next(d for d in details if d["type"]["text"] == "Penalty - Scored")
        self.assertTrue(penalty["penaltyKick"] and penalty["scoringPlay"])
        own_goal = next(d for d in details if d["type"]["text"] == "Own Goal")
        self.assertTrue(own_goal["ownGoal"])

    def test_optional_detail_fields_only_when_present(self):
        details = [d for e in self.events for d in e["competitions"][0]["details"]]
        self.assertFalse(any("corrected" in d or "score" in d for d in details))  # ESPN didn't send them
        raw_detail = copy.deepcopy(next(d for e in self.raw for d in e["competitions"][0]["details"]))
        raw_detail.update(corrected=True, score=2)
        projected = espn.project_detail(raw_detail)
        self.assertEqual((projected["corrected"], projected["score"]), (True, "2"))

    def test_no_competition_key_in_matches(self):
        self.assertNotIn("competitionKey", json.dumps(self.events))


class WindowTest(unittest.TestCase):
    def test_months_mid_month(self):
        self.assertEqual(espn.months_to_fetch(utc(2026, 10, 14, 12), PARIS, 1, 14), ["202610"])

    def test_months_end_of_month(self):
        self.assertEqual(espn.months_to_fetch(utc(2026, 10, 31, 12), PARIS, 1, 14), ["202610", "202611"])
        # J+14 is Oct 31: the window widened by one day also asks for November (ESPN's month boundary is in US time)
        self.assertEqual(espn.months_to_fetch(utc(2026, 10, 17, 12), PARIS, 1, 14), ["202610", "202611"])
        self.assertEqual(espn.months_to_fetch(utc(2026, 10, 16, 12), PARIS, 1, 14), ["202610"])

    def test_months_start_of_month(self):
        # J-1 is in the previous month
        self.assertEqual(espn.months_to_fetch(utc(2026, 11, 1, 12), PARIS, 1, 14), ["202610", "202611"])

    def test_months_year_change(self):
        self.assertEqual(espn.months_to_fetch(utc(2026, 12, 31, 12), PARIS, 1, 14), ["202612", "202701"])

    def test_window_uses_local_days(self):
        start, end = espn.window_bounds(utc(2026, 10, 14, 23, 30), PARIS, 1, 14)  # Already Oct 15 in Paris
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-10-14T00:00:00+02:00", "2026-10-30T00:00:00+01:00"))

    def test_payload_filters_and_sorts(self):
        events = [{"id": "2", "date": "2026-10-20T19:00Z"}, {"id": "1", "date": "2026-10-13T21:00Z"},
                  {"id": "3", "date": "2026-10-12T21:59Z"},  # J-1 starts Oct 13 00:00 Paris = Oct 12 22:00Z
                  {"id": "4", "date": "2026-10-28T22:59Z"},
                  {"id": "5", "date": "2026-10-28T23:00Z"}]  # J+14 = Oct 28, ends at midnight Paris = 23:00Z
        start, end = espn.window_bounds(utc(2026, 10, 14, 12), PARIS, 1, 14)
        self.assertEqual([e["id"] for e in espn.build_matches_payload(events, start, end)["events"]], ["1", "2", "4"])

    def test_live_refresh_detection(self):
        now = utc(2026, 10, 14, 19, 55)
        ev = lambda state, date: {"date": date, "status": {"type": {"state": state}}}
        self.assertTrue(espn.needs_live_refresh(ev("in", "2026-10-14T19:00Z"), now, 10))
        self.assertTrue(espn.needs_live_refresh(ev("pre", "2026-10-14T20:05Z"), now, 10))
        self.assertFalse(espn.needs_live_refresh(ev("pre", "2026-10-14T20:06Z"), now, 10))
        self.assertTrue(espn.needs_live_refresh(ev("pre", "2026-10-14T19:50Z"), now, 10))  # Late kickoff
        self.assertFalse(espn.needs_live_refresh(ev("pre", "2026-10-14T16:00Z"), now, 10))  # Never started
        self.assertFalse(espn.needs_live_refresh(ev("post", "2026-10-14T17:00Z"), now, 10))

    def test_event_days(self):
        self.assertEqual(espn.event_days({"date": "2026-10-14T19:00Z"}, PARIS), ["20261014"])
        self.assertEqual(espn.event_days({"date": "2026-10-15T01:00Z"}, PARIS), ["20261015", "20261014"])


class TeamsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aliases = json.loads((CONFIG / "aliases.json").read_text(encoding="utf-8"))
        cls.ligue1 = load("teams_fra.1.json")
        cls.cl = load("teams_uefa.champions.json")

    def teams(self, payload):
        return [t["team"] for t in payload["sports"][0]["leagues"][0]["teams"]]

    def test_ligue1(self):
        teams = self.teams(espn.build_teams_payload(self.ligue1, "ligue1", "france", self.aliases, {}))
        self.assertEqual(len(teams), len(espn.espn_teams(self.ligue1)))
        for t in teams:
            self.assertEqual(list(t), ["id", "displayName", "abbreviation", "color", "alternateColor",
                                       "competitionKey", "country", "aliases"])
            self.assertEqual((t["competitionKey"], t["country"]), ("ligue1", "france"))
            self.assertRegex(t["color"], HEX)
            self.assertTrue(all(a == a.lower() for a in t["aliases"]))
        psg = next(t for t in teams if t["id"] == "160")
        self.assertIn("psg", psg["aliases"])

    def test_accent_free_name_added_once(self):
        aliases = espn.team_aliases("1", "Atlético Madrid", {"1": {"aliases": ["atleti", "ATM"]}})
        self.assertEqual(aliases, ["atleti", "atm", "atletico madrid"])
        self.assertEqual(espn.team_aliases("1", "Atlético Madrid", {"1": {"aliases": ["atletico madrid"]}}), ["atletico madrid"])
        self.assertEqual(espn.team_aliases("2", "Lille", {}), [])  # No accent, nothing to add

    def test_cup_country_from_national_leagues_else_none(self):
        national = {str(t["id"]): "france" for t in espn.espn_teams(self.ligue1)}
        teams = {t["id"]: t for t in self.teams(espn.build_teams_payload(self.cl, "cl", None, self.aliases, national))}
        self.assertEqual(teams["160"]["country"], "france")       # PSG plays in Ligue 1
        self.assertIsNone(teams["887"]["country"])                 # AEK Athens: no national league configured
        self.assertTrue(all(t["competitionKey"] == "cl" for t in teams.values()))

    def test_country_override_from_aliases(self):
        self.assertEqual(espn.team_country("887", None, {"887": {"country": "Greece"}}, {}), "greece")


class StandingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aliases = json.loads((CONFIG / "aliases.json").read_text(encoding="utf-8"))
        teams = espn.espn_teams(load("teams_fra.1.json"))
        cls.colors = {t["id"]: f"#{t['color']}" for t in teams}

    def test_league_table(self):
        payload = espn.build_standings_payload(load("standings_fra.1.json"), "ligue1", self.colors)
        self.assertEqual((payload["competition"], len(payload["groups"])), ("ligue1", 1))
        rows = payload["groups"][0]["teams"]
        self.assertEqual([r["rank"] for r in rows], list(range(1, 19)))
        self.assertEqual(list(rows[0]), ["rank", "teamId", "abbreviation", "name", "color", "played", "wins", "draws",
                                         "losses", "goalsFor", "goalsAgainst", "goalDifference", "points", "zone"])
        for r in rows:
            self.assertEqual(r["played"], r["wins"] + r["draws"] + r["losses"])
            self.assertEqual(r["goalDifference"], r["goalsFor"] - r["goalsAgainst"])
            self.assertRegex(r["color"], r"^#[0-9A-Fa-f]{6}$")
        self.assertEqual(rows[0]["zone"]["description"], "Champions League")
        self.assertTrue(rows[0]["zone"]["color"].startswith("#"))
        self.assertEqual(rows[-1]["zone"]["description"], "Relegation")

    def test_cup_league_phase_and_knockout_cup(self):
        cl = espn.build_standings_payload(load("standings_uefa.champions.json"), "cl", {})
        self.assertEqual((cl["groups"][0]["name"], len(cl["groups"][0]["teams"])), ("League Phase", 36))
        self.assertIsNone(cl["groups"][0]["teams"][0]["color"])  # No /teams data given
        cup = espn.build_standings_payload(load("standings_fra.coupe_de_france.json"), "coupeDeFrance", {})
        self.assertEqual(cup["groups"], [])

    def test_team_filter(self):
        rows = espn.build_standings_payload(load("standings_fra.1.json"), "ligue1", self.colors, team="psg",
                                            aliases_config=self.aliases)["groups"][0]["teams"]
        self.assertEqual([r["abbreviation"] for r in rows], ["PSG"])


class DayScoreboardTest(unittest.TestCase):
    """/scores and /upcoming on ESPN's all-competitions scoreboard, recorded on Oct 20 (CL live) and for Oct 21."""
    LEAGUES = {"775": "cl", "740": "laliga", "710": "ligue1"}

    @classmethod
    def setUpClass(cls):
        cls.today = load("scoreboard_all_20261020.json")["events"]
        cls.tomorrow = load("scoreboard_all_20261021.json")["events"]
        cls.mixed = load("scoreboard_mixed.json")["events"]
        cls.aliases = json.loads((CONFIG / "aliases.json").read_text(encoding="utf-8"))

    def payload(self, events, **kw):
        return espn.build_day_payload("2026-10-20", events, self.LEAGUES, PARIS, aliases_config=self.aliases, **kw)

    def test_league_id_from_uid(self):
        self.assertEqual(espn.league_id_from_uid("s:600~l:775~e:401915402"), "775")
        self.assertIsNone(espn.league_id_from_uid(""))

    def test_configured_competitions_only_unless_all(self):
        today = self.payload(self.today)
        self.assertEqual((today["date"], today["count"]), ("2026-10-20", 9))
        self.assertTrue(all(g["competition"] == "cl" for g in today["games"]))
        self.assertEqual(self.payload(self.tomorrow)["count"], 10)  # 9 CL + 1 LaLiga
        everything = self.payload(self.today, include_all=True)
        self.assertEqual(everything["count"], len(self.today))
        self.assertIn(None, {g["competition"] for g in everything["games"]})  # Non-configured competitions
        self.assertIn("Liga MX", {g["competitionName"] for g in everything["games"]})

    def test_competition_and_team_filters(self):
        self.assertEqual([g["home"]["abbreviation"] for g in self.payload(self.tomorrow, competition="laliga")["games"]], ["LEV"])
        psg = self.payload(self.today, team="psg")["games"]  # Alias from aliases.json
        self.assertEqual([(g["home"]["abbreviation"], g["away"]["abbreviation"]) for g in psg], [("PSG", "BAR")])
        atm = self.payload(self.today, team="Atletico Madrid")["games"]  # Without the accent
        self.assertEqual([g["away"]["abbreviation"] for g in atm], ["ATM"])
        self.assertEqual(self.payload(self.today, team="nobody")["count"], 0)

    def test_game_format_live_and_not_live(self):
        games = self.payload(self.today)["games"]
        live = next(g for g in games if g["status"] == "in")
        self.assertIsInstance(live["period"], int)
        self.assertTrue(live["clock"] and live["detail"])
        for side in ("home", "away"):
            self.assertRegex(live[side]["color"], r"^#[0-9A-Fa-f]{6}$")  # Colors also during live matches
        done = next(g for g in games if g["status"] == "post")
        self.assertNotIn("clock", done)
        for side in ("home", "away"):
            self.assertRegex(done[side]["color"], r"^#[0-9A-Fa-f]{6}$")
            self.assertIsInstance(done[side]["score"], str)
        self.assertEqual(list(done), ["gameId", "status", "competition", "competitionName", "startTime",
                                      "home", "away", "goals", "cards"])

    def test_start_time_in_paris_24h(self):
        times = {g["gameId"]: g["startTime"] for g in self.payload(self.today)["games"]}
        self.assertEqual(sorted(set(times.values())), ["18:45", "21:00"])  # 16:45Z and 19:00Z (CEST)
        self.assertEqual(espn.format_local_time("2026-10-25T19:00Z", PARIS), "20:00")  # After the switch to CET

    def test_goals_match_the_score(self):
        for e in self.mixed + self.today:
            if e["status"]["type"]["state"] != "post":
                continue
            goals, _ = espn.goals_and_cards(e)
            for c in e["competitions"][0]["competitors"]:
                scored = [g for g in goals if g["team"] == c["team"]["abbreviation"]]
                self.assertEqual(len(scored), int(c["score"]), f"{e['name']} {c['team']['abbreviation']}")

    def test_goal_and_card_details(self):
        by_name = {e["name"]: espn.goals_and_cards(e) for e in self.mixed}
        goals, cards = by_name["Strasbourg at Nice"]
        own = next(g for g in goals if g["ownGoal"])
        self.assertEqual((own["team"], own["player"], own["minute"]), ("NICE", "P. Diop", "45'+2'"))  # Credited to Nice
        goals, _ = by_name["Toulouse at AS Monaco"]
        penalty = next(g for g in goals if g["penalty"])
        self.assertEqual(penalty["type"], "Penalty - Scored")
        _, cards = by_name["Lyon at Lens"]
        self.assertIn("red", {c["card"] for c in cards})
        self.assertIn("yellow", {c["card"] for c in cards})
        for g in goals:
            self.assertEqual(list(g), ["minute", "team", "player", "type", "penalty", "ownGoal"])
        for c in cards:
            self.assertEqual(list(c), ["minute", "team", "player", "card"])

    def test_shootout_kicks_are_not_goals(self):
        event = copy.deepcopy(next(e for e in self.mixed if e["name"] == "Toulouse at AS Monaco"))
        details = event["competitions"][0]["details"]
        before = len(espn.goals_and_cards(event)[0])
        details.append({**copy.deepcopy(next(d for d in details if d.get("scoringPlay"))), "shootout": True})
        self.assertEqual(len(espn.goals_and_cards(event)[0]), before)


if __name__ == "__main__":
    unittest.main()
