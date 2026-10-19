"""Small, source-independent regressions for the real bxcj_demo path."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from dlib.trace_pipeline import WeintekCsvTraceReader
from lib.decoders.smc2_decoder import SMC2Decoder
from lib.step_logic_slicer import StepLogicSlicer
from lib.step_graph import steps_raw_to_step_graph


class SourceAndIdentityTests(unittest.TestCase):
    def test_smc2_ix_scoped_to_network(self):
        decoder = SMC2Decoder()
        a = decoder._make_element_id({"Ix": 10}, "0:1", 0)
        b = decoder._make_element_id({"Ix": 10}, "0:2", 0)
        self.assertNotEqual(a, b)
        self.assertIn("0:1", a)
        self.assertIn("0:2", b)

    def test_rail_references_resolve_qualified_ids(self):
        decoder = SMC2Decoder()
        cls = [
            {"__type": "LD", "Ix": 10, "X": 0, "Y": 0},
            {"__type": "ST", "Ix": 20, "X": 1, "Y": 0},
        ]
        ids = {i: decoder._make_element_id(cl, "7:2", i)
               for i, cl in enumerate(cls)}
        edges = decoder._build_rung_connections(
            cls, "7:2", "test.smc2", lri=10, rri=20, id_map=ids
        )
        self.assertTrue(any(e.connection_type == "power_rail_in"
                            and e.target_element_id == ids[0] for e in edges))
        self.assertTrue(any(e.connection_type == "power_rail_out"
                            and e.source_element_id == ids[1] for e in edges))

    def test_state_variable_prefix_is_not_another_family(self):
        slicer = StepLogicSlicer({"state_variables": [
            {"name": "m_nNextAutoStep"}, {"name": "m_nNextAutoStep_load_Up"}
        ]})
        # Separate, single-family config must never claim a sibling family.
        simple = StepLogicSlicer({"state_variables": [
            {"name": "m_nNextAutoStep"}
        ]})
        self.assertIsNotNone(slicer.state_var_re.search("m_nNextAutoStep_load_Up[3]"))
        self.assertIsNone(simple.state_var_re.search("m_nNextAutoStep_load_Up[3]"))
        self.assertIsNotNone(simple.state_var_re.search("m_nNextAutoStep[3]"))

    def test_step_graph_uses_written_target_family(self):
        raw = [{"state_variable": "m_nNextAutoStep", "station": "S1",
                "thread_idx": 3, "step_value": 110,
                "guards": [], "actions": [], "calls": [], "timers": [],
                "common_enables": [], "st_code": [], "comment": "",
                "transitions": [{"target_thread": 3, "target_value": 120,
                                 "target_state_variable": "m_nNextAutoStep_load_Up",
                                 "condition": "Ready", "source": "topology_slice"}]}]
        graph = steps_raw_to_step_graph(raw)
        self.assertEqual(graph.steps[0].outgoing_transitions[0].target.state_variable,
                         "m_nNextAutoStep_load_Up")

    def test_source_scan_includes_smc2_and_rejects_empty(self):
        module_path = ROOT / "vendor" / "tools" / "build_step_graph.py"
        spec = importlib.util.spec_from_file_location("build_graph_test", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / "source").mkdir()
            f = project / "source" / "Mylar.smc2"
            f.write_text("<smc2/>", encoding="utf-8")
            config = {"source_decoder": "smc2"}
            with patch.object(module, "SMC2Decoder") as ctor:
                ctor.return_value.decode.return_value = SimpleNamespace(
                    program_units=[SimpleNamespace(name="unit")]
                )
                items = module.decode_sources(project, config)
                self.assertEqual(len(items), 1)
                self.assertEqual(Path(ctor.return_value.decode.call_args.args[0]), f)
                ctor.return_value.decode.return_value = SimpleNamespace(program_units=[])
                with self.assertRaises(ValueError):
                    module.decode_sources(project, config)


class CsvTimestampTests(unittest.TestCase):
    def test_csv_clock_time_is_ms_not_seconds(self):
        reader = WeintekCsvTraceReader(Path("unused.csv"))
        a = reader._parse_clock_time("2026-09-04", "14:12:07.49")
        b = reader._parse_clock_time("2026-09-04", "14:12:07.50")
        self.assertAlmostEqual(b - a, 10.0, delta=0.001)

    def test_bad_clock_time_is_not_zero_epoch(self):
        reader = WeintekCsvTraceReader(Path("unused.csv"))
        self.assertIsNone(reader._parse_clock_time("bad", "clock"))


if __name__ == "__main__":
    unittest.main()
