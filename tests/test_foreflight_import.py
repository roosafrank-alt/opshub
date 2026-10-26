"""ForeFlight logbook import: parser, preview, confirm, duplicate handling, permissions."""
import io
import os
import re

from harness import OpsHubTestCase
import db
import foreflight_import as ff

HEAD = ("Date,AircraftID,From,To,Route,TimeOut,TimeOff,TimeOn,TimeIn,OnDuty,OffDuty,TotalTime,PIC,SIC,Night,Solo,CrossCountry,PICUS,MultiPilot,IFR,"
        "Examiner,NVG,NVG Ops,Distance,ActualInstrument,SimulatedInstrument,HobbsStart,HobbsEnd,TachStart,TachEnd,Holds,Approach1,Approach2,Approach3,"
        "Approach4,Approach5,Approach6,DualGiven,DualReceived,SimulatedFlight,GroundTraining,GroundTrainingGiven,InstructorName,InstructorComments,"
        "Person1,Person2,Person3,Person4,Person5,Person6,PilotComments,Flight Review (FAA),IPC (FAA),Checkride (FAA),FAA 61.58 (FAA),NVG Proficiency (FAA),"
        "Takeoff Day,Takeoff Night,Landing Full-Stop Day,Landing Full-Stop Night,Landing Full-Stop Day Towered,Landing Full-Stop Night Towered,DayTakeoffs,"
        "DayLandingsFullStop,NightTakeoffs,NightLandingsFullStop,AllLandings,[Counter]Instrument Approach,[Toggle]International,[Hours]Seat Time,[Hours]Widebody")
COLS = HEAD.split(",")


def row(**kw):
    d = {c: "" for c in COLS}
    d.update(TotalTime="0.0", PIC="0.0", SIC="0.0", Night="0.0", Solo="0.0", CrossCountry="0.0", DualGiven="0.0", DualReceived="0.0",
           DayLandingsFullStop="1", AllLandings="1", NightLandingsFullStop="0")
    d.update(kw)
    return ",".join(('"%s"' % d[c]) if "," in d[c] else d[c] for c in COLS)


def export(*rows):
    return ("ForeFlight Logbook Import,This row is required for importing into ForeFlight. Do not delete or modify.\n\n"
            "Aircraft Table,,,\nAircraftID,TypeCode,Year,Make,Model,GearType,EngineType\n"
            "N5569P,C172,1975,Cessna Aircraft,172M Skyhawk,fixed_tricycle,Piston\nN476MC,B744,2001,Boeing,747-400F,retractable_tricycle,Jet\n\n"
            "Flights Table , , ,Deprecated: Do not edit manually\n" + HEAD + "\n" + "\n".join(rows) + "\n")


FLIGHTS = [
    row(Date="2026-10-20", AircraftID="N5569P", From="N89", To="KPOU", TotalTime="1.4", PIC="1.4", Solo="1.4", CrossCountry="1.4", Night="0.2",
        PilotComments="Solo XC, nice day", Person1="Kate Kearney;Instructor;kate@example.com;", AllLandings="2", NightLandingsFullStop="1"),
    row(Date="2026-10-21", AircraftID="N476MC", From="EBLG", To="KJFK", TotalTime="8.3", SIC="8.3", CrossCountry="8.3", PilotComments="THY6044"),
    row(Date="2026-10-19", AircraftID="N5569P", From="N89", To="N89", TotalTime="1.0", DualReceived="1.0", InstructorName="K Kearney"),
    row(Date="2026-10-18", AircraftID="N5569P", From="N89", To="N89", TotalTime="0.0"),                  # nothing logged
    row(Date="2999-01-01", AircraftID="N5569P", From="N89", To="N89", TotalTime="1.0"),                  # future
    row(Date="2026-10-17", AircraftID="N5569P", From="N89", To="N89", TotalTime="0.0", SimulatedFlight="1.5"),  # simulator only
]


class ForeFlightParse(OpsHubTestCase):
    def test_reads_flights_aircraft_and_skips_junk(self):
        r = ff.parse(export(*FLIGHTS))
        self.assertIsNone(r["problem"])
        self.assertEqual(len(r["entries"]), 4)
        self.assertEqual(sorted(s[1] for s in r["skipped"]), ["date is in the future", "no time logged"])
        e = {x["entry_date"]: x for x in r["entries"]}
        self.assertEqual(e["2026-10-20"]["make_model"], "Cessna 172M Skyhawk")      # "Aircraft" dropped from the make
        self.assertEqual(e["2026-10-20"]["remarks"], "Solo XC, nice day")           # quotes stripped
        self.assertEqual((e["2026-10-20"]["night_ldg"], e["2026-10-20"]["day_ldg"]), (1, 1))
        self.assertEqual(e["2026-10-21"]["sic"], 8.3)
        self.assertEqual(e["2026-10-19"]["dual"], 1.0)
        self.assertEqual(e["2026-10-19"]["instructor_name"], "K Kearney")
        self.assertEqual(e["2026-10-17"]["sim_flight"], 1.5)
        self.assertNotIn("kate@example.com", str(r["entries"]))                      # other people's details never come in
        self.assertEqual(ff.totals(r["entries"])["total"], 10.7)

    def test_rejects_other_files(self):
        self.assertIn("doesn't look like", ff.parse("Date,Tail\n2026-01-01,N1\n")["problem"])
        self.assertIn("Flights Table", ff.parse("ForeFlight Logbook Import,x\n")["problem"])

    def test_same_flight_same_key(self):
        a = ff.parse(export(FLIGHTS[0]))["entries"][0]
        b = ff.parse(export(FLIGHTS[1], FLIGHTS[0]))["entries"][1]
        self.assertEqual(a["import_key"], b["import_key"])


class ForeFlightImport(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")

    def _upload(self, c, text, **q):
        return c.post("/academy/logbook/import", data={"file": (io.BytesIO(text.encode()), "logbook.csv")},
                      content_type="multipart/form-data", query_string=q)

    def _confirm(self, c, html, **extra):
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        return c.post("/academy/logbook/import", data=dict(action="confirm", token=token, **extra))

    def test_preview_then_import_then_reupload_adds_nothing(self):
        c = self.login("flight_student")
        html = self._upload(c, export(*FLIGHTS)).get_data(as_text=True)
        self.assertIn("4 new flights", html)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 0)   # preview saves nothing
        r = self._confirm(c, html)
        self.assertEqual(r.status_code, 302)
        rows = self.q("SELECT * FROM pilot_logbook WHERE source='foreflight' ORDER BY entry_date")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(x["status"] == "complete" and x["approved_by"] == "Imported from ForeFlight" for x in rows))
        self.assertEqual(round(sum(x["sic"] or 0 for x in rows), 1), 8.3)
        again = self._upload(c, export(*FLIGHTS)).get_data(as_text=True)
        self.assertIn("Nothing new to import", again)
        self.assertIn("already imported", again)
        page = c.get("/academy/logbook").get_data(as_text=True)
        self.assertIn("Imported", page)

    def test_flight_the_school_already_logged_is_held_back(self):
        c = self.login("flight_student")
        sid = self.users["flight_student"]["id"]
        stu = self.q("SELECT id FROM students WHERE user_id = ?", (sid,))[0]["id"]
        self.exec("INSERT INTO pilot_logbook (student_id, entry_date, tail, total, status) VALUES (?,?,?,?,?)", (stu, "2026-10-20", "N5569P", 1.3, "complete"))
        html = self._upload(c, export(FLIGHTS[0], FLIGHTS[1])).get_data(as_text=True)
        self.assertIn("look like ones the school already logged", html)
        self._confirm(c, html)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 1)      # only the airline flight
        html = self._upload(c, export(FLIGHTS[0])).get_data(as_text=True)
        self._confirm(c, html, include_maybe="1")
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 2)

    def test_bad_files_save_nothing(self):
        c = self.login("flight_student")
        self.assertIn("doesn", self._upload(c, "a,b\n1,2\n").get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 0)

    def test_only_the_student_themselves_can_import(self):
        c = self.login("cfi")
        sid = self.q("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))[0]["id"]
        r = c.get("/academy/logbook/import", query_string={"student_id": sid})
        self.assertEqual(r.status_code, 302)                                                     # a CFI reads, never edits
        self.assertNotIn("Import from ForeFlight", c.get("/academy/logbook", query_string={"student_id": sid}).get_data(as_text=True))
        self.assertIn("Import from ForeFlight", self.login("flight_student").get("/academy/logbook").get_data(as_text=True))
        # an admin (flight or master) can look at a student's logbook but never import into it
        for who in ("flight_admin", "master"):
            a = self.login(who)
            self.assertEqual(a.get("/academy/logbook/import", query_string={"student_id": sid}).status_code, 302)
            self.assertNotIn("Import from ForeFlight", a.get("/academy/logbook", query_string={"student_id": sid}).get_data(as_text=True))
            r = a.post("/academy/logbook/import", data={"action": "confirm", "token": "x" * 20, "student_id": sid})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 0)

    def test_someone_elses_preview_token_is_refused(self):
        c = self.login("flight_student")
        html = self._upload(c, export(FLIGHTS[0])).get_data(as_text=True)
        other = self.login("master")
        self.assertIn(other.post("/academy/logbook/import", data=dict(action="confirm", token=re.search(r'name="token" value="([^"]+)"', html).group(1))).status_code, (302, 403))
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 0)

    def test_overlapping_files_only_add_what_is_new(self):
        c = self.login("flight_student")
        a = export(FLIGHTS[0], FLIGHTS[1])
        self._confirm(c, self._upload(c, a).get_data(as_text=True))
        # a newer export: the same two flights (one with an edited comment) plus a new one and a twin of a new one
        twin = row(Date="2026-10-22", AircraftID="N5569P", From="N89", To="N89", TotalTime="0.5", PIC="0.5")
        b = export(FLIGHTS[0].replace("nice day", "great day"), FLIGHTS[1], twin, twin)
        html = self._upload(c, b).get_data(as_text=True)
        self.assertIn("2 new flights", html)
        self.assertIn("2 flights already imported", html)
        self._confirm(c, html)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM pilot_logbook WHERE source='foreflight'")[0]["n"], 4)
        self.assertIn("Nothing new to import", self._upload(c, b).get_data(as_text=True))
