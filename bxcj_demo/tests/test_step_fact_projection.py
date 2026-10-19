"""Regression checks for step projection; no PLC library or Gold data needed."""
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
from lib.step_fact_projection import (
    boolean_to_string, slice_to_step_raw, choose_primary_slice, step_key,
)


@dataclass
class Fact:
    fact_type: str = "call"
    variable: str = ""
    callee: str = "AxisMove"
    inputs: list = field(default_factory=lambda: [{"name": "_Axis", "value_str": "Axis009Program"}, {"name": "_Position", "value_str": "3"}])
    outputs: list = field(default_factory=list)
    provenance: str = "topology_only"
    evidence_path: list = field(default_factory=lambda: [{"element_id": "11", "edge_type": "topology:wire"}])
    target_step: dict = field(default_factory=dict)
    boolean_expr: dict = field(default_factory=dict)
    element_id: str = "11"
    network_id: str = "N2"

@dataclass
class Slice:
    step_identity: dict = field(default_factory=lambda: {"state_variable": "m_nNextAutoStep", "station": "", "thread_idx": 3, "step_value": 110})
    anchor_element_id: str = "4"
    anchor_network_id: str = "N2"
    anchor_unit_name: str = "run"
    source_file: str = "source.smc2"
    comment: str = "110-电芯上料A上下电机"
    guards: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    calls: list = field(default_factory=lambda: [Fact()])
    transitions: list = field(default_factory=list)
    timers: list = field(default_factory=list)
    common_enables: list = field(default_factory=list)
    unresolved_upstream: list = field(default_factory=list)
    unresolved_logic: list = field(default_factory=list)
    boolean_expression: dict = field(default_factory=dict)

class ProjectionTests(unittest.TestCase):
    def test_preserves_fb_parameter_and_provenance(self):
        item = slice_to_step_raw(Slice())
        self.assertEqual(item["calls"][0]["inputs"][0]["value_str"], "Axis009Program")
        self.assertEqual(item["calls"][0]["evidence_path"][0]["element_id"], "11")

    def test_state_family_identity(self):
        a = {"state_variable": "m_nNextAutoStep", "thread_idx": 3, "step_value": 110}
        b = {"state_variable": "m_nNextAutoStep_load_Up", "thread_idx": 3, "step_value": 110}
        self.assertNotEqual(step_key(a), step_key(b))

    def test_ambiguous_duplicate_is_explicit(self):
        a = Slice()
        b = Slice(anchor_network_id="N3")
        selected, status = choose_primary_slice([a,b])
        self.assertIn(selected, (a,b))
        self.assertEqual(status, "needs_owner_verification")

    def test_boolean_parentheses(self):
        expr = {"expr_type":"and","children":[
            {"expr_type":"or","children":[{"expr_type":"atom","variable":"A"},{"expr_type":"atom","variable":"B"}]},
            {"expr_type":"atom","variable":"C"}
        ]}
        self.assertEqual(boolean_to_string(expr), "(A OR B) AND C")

    def test_unknown_branch_does_not_disappear(self):
        expr = {"expr_type":"or","children":[{"expr_type":"atom","variable":"A"},{"expr_type":"unknown"}]}
        self.assertEqual(boolean_to_string(expr), "")

if __name__ == "__main__":
    unittest.main()
