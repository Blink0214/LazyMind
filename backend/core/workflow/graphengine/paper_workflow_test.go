package graphengine

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func paperGraph(t *testing.T, edit func(string) string) *CompiledStateGraph {
	t.Helper()
	root := filepath.Join("..", "..", "..", "..", "workflows", "paper-writing")
	workflow, err := os.ReadFile(filepath.Join(root, "workflow.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	state, err := os.ReadFile(filepath.Join(root, "scenario/state.yml"))
	if err != nil {
		t.Fatal(err)
	}
	body := string(workflow)
	if edit != nil {
		body = edit(body)
	}
	result := Compile(body, string(state), "", ProfileRuntimeLoad)
	if !result.Valid {
		t.Fatalf("invalid paper graph: %+v", result.Diagnostics)
	}
	return result.Graph
}

func TestPaperGraphAllForwardScenarios(t *testing.T) {
	graph := paperGraph(t, nil)
	paths := [][]string{
		{"research_topic", "literature_review", "refine_topic"},
		{"literature_review", "write_document", "peer_review", "revise_document"},
		{"data_analysis", "write_document"}, {"reading_reflection"}, {"literature_review", "reading_reflection"},
		{"literature_review", "experiment", "data_analysis"},
	}
	for _, node := range []string{"research_topic", "literature_review", "experiment", "data_analysis", "write_document", "peer_review", "reading_reflection"} {
		paths = append(paths, []string{node})
	}
	for _, path := range paths {
		t.Run(strings.Join(path, "-"), func(t *testing.T) {
			snapshot := RuntimeSnapshot{}
			steps := append([]string{"prepare"}, path...)
			steps = append(steps, "finish")
			previous := "__start__"
			for _, step := range steps {
				choice := SelectRouteTarget(graph, previous, step, DecideRoute(graph, previous, snapshot.Materials))
				snapshot.Routes = append(snapshot.Routes, RouteFact{
					From: previous, Activated: choice.Activated, Pruned: choice.Pruned, Validity: "effective",
				})
				projected := Project(graph, snapshot)
				if projected.Nodes[step].Reachability != "reachable" || projected.Nodes[step].Readiness != "ready" {
					t.Fatalf("%s not executable: %+v", step, projected.Nodes[step])
				}
				snapshot.Attempts = append(snapshot.Attempts, AttemptFact{StepID: step, Status: "succeeded", Validity: "effective"})
				for _, slot := range graph.Nodes[step].Outputs {
					snapshot.Materials = append(snapshot.Materials, MaterialValue{MaterialID: slot, RevisionID: slot + "-1", Valid: true})
				}
				previous = step
			}
		})
	}
}

func TestRecoveryPolicyValidationAndLegacyHash(t *testing.T) {
	plugin := "id: recovery-test\nslots:\n  - {id: feedback, type: text, cardinality: single}\n  - {id: input, type: text, cardinality: single, external: true}\nsteps:\n  - {id: draft}\n"
	state := "steps:\n  draft:\n    inputs: [{material: input, required: false}]\n    outputs: [feedback]\ntransitions:\n  __start__: [{to: draft}]\n  draft: [{to: __end__}]\n"
	legacy := Compile(plugin, state, "", ProfileRuntimeLoad)
	empty := Compile(plugin+"runtime: {recovery_inputs: {}}\n", state, "", ProfileRuntimeLoad)
	if !legacy.Valid || !empty.Valid || legacy.Graph.GraphHash != empty.Graph.GraphHash {
		t.Fatal("empty opt-in changes legacy graph hash")
	}
	valid := Compile(plugin+"runtime:\n  recovery_inputs:\n    draft: {input: feedback}\n", state, "", ProfileRuntimeLoad)
	if !valid.Valid || valid.Graph.Runtime.IsZero() {
		t.Fatalf("policy not retained: %+v", valid.Diagnostics)
	}
	for _, binding := range []string{
		"unknown: {input: feedback}", "draft: {feedback: input}", "draft: {input: missing}", "draft: {missing: feedback}",
	} {
		result := Compile(plugin+"runtime:\n  recovery_inputs:\n    "+binding+"\n", state, "", ProfileRuntimeLoad)
		if result.Valid {
			t.Fatalf("invalid policy accepted: %s", binding)
		}
	}
}
