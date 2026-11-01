package measure

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"
)

func TestNewSimulator(t *testing.T) {
	sim := NewSimulator()

	// All tracked PCRs should be initialized to zeros.
	expectedPCRs := []int{0, 1, 2, 3, 4, 7, 8, 9, 11, 12, 13, 14, 15}
	for _, idx := range expectedPCRs {
		val, ok := sim.Bank[idx]
		if !ok {
			t.Errorf("PCR %d not initialized", idx)
			continue
		}
		if val != [32]byte{} {
			t.Errorf("PCR %d not zero-initialized: got %x", idx, val)
		}
	}

	// PCRs not in the tracked set should not exist.
	for _, idx := range []int{5, 6, 10, 16} {
		if _, ok := sim.Bank[idx]; ok {
			t.Errorf("PCR %d should not exist", idx)
		}
	}
}

func TestExtend(t *testing.T) {
	sim := NewSimulator()

	digest := sha256.Sum256([]byte("test data"))
	sim.Extend(4, digest, "EV_TEST", "test event")

	// The new value should be SHA256(zeros || digest).
	var zeros [32]byte
	combined := append(zeros[:], digest[:]...)
	expected := sha256.Sum256(combined)

	if sim.Bank[4] != expected {
		t.Errorf("PCR 4 after extend: got %x, want %x", sim.Bank[4], expected)
	}

	// Event log should have one entry.
	if len(sim.EventLog.Events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(sim.EventLog.Events))
	}

	ev := sim.EventLog.Events[0]
	if ev.PCR != 4 {
		t.Errorf("event PCR: got %d, want 4", ev.PCR)
	}
	if ev.Type != "EV_TEST" {
		t.Errorf("event type: got %q, want %q", ev.Type, "EV_TEST")
	}
	if ev.Desc != "test event" {
		t.Errorf("event description: got %q, want %q", ev.Desc, "test event")
	}
	if ev.Digest != hex.EncodeToString(digest[:]) {
		t.Errorf("event digest mismatch")
	}
}

func TestExtendChaining(t *testing.T) {
	sim := NewSimulator()

	d1 := sha256.Sum256([]byte("first"))
	d2 := sha256.Sum256([]byte("second"))

	sim.Extend(7, d1, "EV_TEST", "first")
	sim.Extend(7, d2, "EV_TEST", "second")

	// Manually compute expected value.
	var zeros [32]byte
	step1 := sha256.Sum256(append(zeros[:], d1[:]...))
	step2 := sha256.Sum256(append(step1[:], d2[:]...))

	if sim.Bank[7] != step2 {
		t.Errorf("PCR 7 after two extends: got %x, want %x", sim.Bank[7], step2)
	}

	if len(sim.EventLog.Events) != 2 {
		t.Errorf("expected 2 events, got %d", len(sim.EventLog.Events))
	}
}

func TestMarshalJSON(t *testing.T) {
	sim := NewSimulator()
	digest := sha256.Sum256([]byte("test"))
	sim.Extend(4, digest, "EV_TEST", "test")

	data, err := sim.MarshalJSON()
	if err != nil {
		t.Fatalf("MarshalJSON error: %v", err)
	}

	// Verify it is valid JSON.
	var result map[string]json.RawMessage
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}

	// Should have "measurements" and "eventLog" keys.
	if _, ok := result["measurements"]; !ok {
		t.Error("missing 'measurements' key")
	}
	if _, ok := result["eventLog"]; !ok {
		t.Error("missing 'eventLog' key")
	}

	// Parse measurements.
	var measurements map[string]struct {
		Expected string `json:"expected"`
	}
	if err := json.Unmarshal(result["measurements"], &measurements); err != nil {
		t.Fatalf("parsing measurements: %v", err)
	}

	// PCR 4 should be present and non-zero.
	pcr4, ok := measurements["4"]
	if !ok {
		t.Fatal("PCR 4 not in measurements")
	}
	if pcr4.Expected == hex.EncodeToString(make([]byte, 32)) {
		t.Error("PCR 4 should not be zero after extend")
	}

	// Should have "rtmr" key.
	if _, ok := result["rtmr"]; !ok {
		t.Error("missing 'rtmr' key")
	}

	// Parse RTMR values.
	var rtmrValues map[string]struct {
		Expected string `json:"expected"`
	}
	if err := json.Unmarshal(result["rtmr"], &rtmrValues); err != nil {
		t.Fatalf("parsing rtmr: %v", err)
	}

	// All 4 RTMRs should be present.
	for _, idx := range []string{"0", "1", "2", "3"} {
		if _, ok := rtmrValues[idx]; !ok {
			t.Errorf("missing RTMR[%s] in output", idx)
		}
	}

	// PCR 4 maps to RTMR[1], so RTMR[1] should be non-zero.
	zeros48Hex := hex.EncodeToString(make([]byte, 48))
	if rtmrValues["1"].Expected == zeros48Hex {
		t.Error("RTMR[1] should not be zero after PCR 4 extend")
	}

	// Parse event log.
	var events []Event
	if err := json.Unmarshal(result["eventLog"], &events); err != nil {
		t.Fatalf("parsing eventLog: %v", err)
	}
	if len(events) != 1 {
		t.Errorf("expected 1 event, got %d", len(events))
	}
}
