package measure

import (
	"crypto/sha256"
	"crypto/sha512"
	"encoding/hex"
	"encoding/json"
	"testing"
)

func TestPCRToRTMR(t *testing.T) {
	tests := []struct {
		pcr      int
		expected int
	}{
		{0, -1},  // MRTD, not runtime-extendable
		{1, 0},   // firmware config
		{2, 1},   // boot loader chain
		{3, 1},   // boot loader chain
		{4, 1},   // boot loader chain
		{5, 1},   // boot loader chain
		{6, 1},   // boot loader chain
		{7, 0},   // firmware config
		{8, 2},   // OS measurements
		{9, 2},   // OS measurements
		{10, 2},  // OS measurements
		{11, 2},  // OS measurements
		{12, 2},  // OS measurements
		{13, 2},  // OS measurements
		{14, 2},  // OS measurements
		{15, 2},  // OS measurements
		{16, -1}, // out of range
		{-1, -1}, // negative
	}

	for _, tc := range tests {
		got := PCRToRTMR(tc.pcr)
		if got != tc.expected {
			t.Errorf("PCRToRTMR(%d) = %d, want %d", tc.pcr, got, tc.expected)
		}
	}
}

func TestRTMRExtend(t *testing.T) {
	bank := make(RTMRBank)
	bank[0] = [48]byte{} // starts at zero

	// Create a known digest (zero-padded SHA-256 to 48 bytes).
	sha256Digest := sha256.Sum256([]byte("test data"))
	var digest384 [48]byte
	copy(digest384[:32], sha256Digest[:])

	RTMRExtend(bank, 0, digest384)

	// Manually compute expected: SHA384(zeros48 || digest384).
	var zeros48 [48]byte
	combined := make([]byte, 96)
	copy(combined[:48], zeros48[:])
	copy(combined[48:], digest384[:])
	expected := sha512.Sum384(combined)

	if bank[0] != expected {
		t.Errorf("RTMRExtend: got %x, want %x", bank[0], expected)
	}
}

func TestRTMRExtendChaining(t *testing.T) {
	bank := make(RTMRBank)
	bank[0] = [48]byte{}

	d1 := sha256.Sum256([]byte("first"))
	d2 := sha256.Sum256([]byte("second"))

	var digest1 [48]byte
	copy(digest1[:32], d1[:])
	var digest2 [48]byte
	copy(digest2[:32], d2[:])

	RTMRExtend(bank, 0, digest1)
	RTMRExtend(bank, 0, digest2)

	// Manually chain: step1 = SHA384(zeros || d1), step2 = SHA384(step1 || d2).
	var zeros48 [48]byte
	c1 := make([]byte, 96)
	copy(c1[:48], zeros48[:])
	copy(c1[48:], digest1[:])
	step1 := sha512.Sum384(c1)

	c2 := make([]byte, 96)
	copy(c2[:48], step1[:])
	copy(c2[48:], digest2[:])
	step2 := sha512.Sum384(c2)

	if bank[0] != step2 {
		t.Errorf("RTMRExtend chaining: got %x, want %x", bank[0], step2)
	}
}

func TestDeriveRTMRs(t *testing.T) {
	// Build an event log with events for different PCRs.
	d4 := sha256.Sum256([]byte("pcr4 event"))
	d9 := sha256.Sum256([]byte("pcr9 event"))
	d1 := sha256.Sum256([]byte("pcr1 event"))
	d0 := sha256.Sum256([]byte("pcr0 event"))

	eventLog := EventLog{
		Events: []Event{
			{PCR: 0, Type: "EV_TEST", Digest: hex.EncodeToString(d0[:]), Desc: "pcr0"},
			{PCR: 4, Type: "EV_TEST", Digest: hex.EncodeToString(d4[:]), Desc: "pcr4"},
			{PCR: 9, Type: "EV_TEST", Digest: hex.EncodeToString(d9[:]), Desc: "pcr9"},
			{PCR: 1, Type: "EV_TEST", Digest: hex.EncodeToString(d1[:]), Desc: "pcr1"},
		},
	}

	bank := DeriveRTMRs(eventLog)

	// Verify all 4 RTMRs exist.
	for i := 0; i < 4; i++ {
		if _, ok := bank[i]; !ok {
			t.Errorf("RTMR[%d] not present in bank", i)
		}
	}

	// PCR 0 -> skipped (MRTD). No events affect it directly via RTMR.

	// PCR 4 -> RTMR[1]: one event with d4.
	var d4Padded [48]byte
	copy(d4Padded[:32], d4[:])
	var zeros48 [48]byte
	c := make([]byte, 96)
	copy(c[:48], zeros48[:])
	copy(c[48:], d4Padded[:])
	expectedRTMR1 := sha512.Sum384(c)

	if bank[1] != expectedRTMR1 {
		t.Errorf("RTMR[1] mismatch: got %x, want %x", bank[1], expectedRTMR1)
	}

	// PCR 9 -> RTMR[2]: one event with d9.
	var d9Padded [48]byte
	copy(d9Padded[:32], d9[:])
	c2 := make([]byte, 96)
	copy(c2[:48], zeros48[:])
	copy(c2[48:], d9Padded[:])
	expectedRTMR2 := sha512.Sum384(c2)

	if bank[2] != expectedRTMR2 {
		t.Errorf("RTMR[2] mismatch: got %x, want %x", bank[2], expectedRTMR2)
	}

	// PCR 1 -> RTMR[0]: one event with d1.
	var d1Padded [48]byte
	copy(d1Padded[:32], d1[:])
	c3 := make([]byte, 96)
	copy(c3[:48], zeros48[:])
	copy(c3[48:], d1Padded[:])
	expectedRTMR0 := sha512.Sum384(c3)

	if bank[0] != expectedRTMR0 {
		t.Errorf("RTMR[0] mismatch: got %x, want %x", bank[0], expectedRTMR0)
	}

	// RTMR[3] should remain zero (no PCR maps to it).
	if bank[3] != zeros48 {
		t.Errorf("RTMR[3] should be zero, got %x", bank[3])
	}
}

func TestDeriveRTMRsMultipleEventsPerRTMR(t *testing.T) {
	// Two events that both map to RTMR[1] (PCR 2 and PCR 4).
	d2 := sha256.Sum256([]byte("pcr2 event"))
	d4 := sha256.Sum256([]byte("pcr4 event"))

	eventLog := EventLog{
		Events: []Event{
			{PCR: 2, Type: "EV_TEST", Digest: hex.EncodeToString(d2[:]), Desc: "pcr2"},
			{PCR: 4, Type: "EV_TEST", Digest: hex.EncodeToString(d4[:]), Desc: "pcr4"},
		},
	}

	bank := DeriveRTMRs(eventLog)

	// Both map to RTMR[1], so chain: SHA384(SHA384(zeros || d2pad) || d4pad).
	var d2Padded, d4Padded [48]byte
	copy(d2Padded[:32], d2[:])
	copy(d4Padded[:32], d4[:])

	var zeros48 [48]byte
	c1 := make([]byte, 96)
	copy(c1[:48], zeros48[:])
	copy(c1[48:], d2Padded[:])
	step1 := sha512.Sum384(c1)

	c2 := make([]byte, 96)
	copy(c2[:48], step1[:])
	copy(c2[48:], d4Padded[:])
	expected := sha512.Sum384(c2)

	if bank[1] != expected {
		t.Errorf("RTMR[1] with two events: got %x, want %x", bank[1], expected)
	}
}

func TestDeriveRTMRsEmptyEventLog(t *testing.T) {
	bank := DeriveRTMRs(EventLog{})

	var zeros48 [48]byte
	for i := 0; i < 4; i++ {
		if bank[i] != zeros48 {
			t.Errorf("RTMR[%d] should be zero for empty event log, got %x", i, bank[i])
		}
	}
}

func TestSimulatorJSONIncludesRTMR(t *testing.T) {
	sim := NewSimulator()

	// Extend PCR 4 (maps to RTMR[1]) and PCR 9 (maps to RTMR[2]).
	d4 := sha256.Sum256([]byte("boot event"))
	d9 := sha256.Sum256([]byte("os event"))
	sim.Extend(4, d4, "EV_TEST", "boot")
	sim.Extend(9, d9, "EV_TEST", "os")

	data, err := sim.MarshalJSON()
	if err != nil {
		t.Fatalf("MarshalJSON error: %v", err)
	}

	var result map[string]json.RawMessage
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}

	// Check "rtmr" key exists.
	rtmrRaw, ok := result["rtmr"]
	if !ok {
		t.Fatal("missing 'rtmr' key in JSON output")
	}

	var rtmrValues map[string]struct {
		Expected string `json:"expected"`
	}
	if err := json.Unmarshal(rtmrRaw, &rtmrValues); err != nil {
		t.Fatalf("parsing rtmr: %v", err)
	}

	// All 4 RTMRs should be present.
	for _, idx := range []string{"0", "1", "2", "3"} {
		if _, ok := rtmrValues[idx]; !ok {
			t.Errorf("missing RTMR[%s] in output", idx)
		}
	}

	// RTMR[1] should be non-zero (PCR 4 event mapped to it).
	zeros96 := hex.EncodeToString(make([]byte, 48))
	if rtmrValues["1"].Expected == zeros96 {
		t.Error("RTMR[1] should not be zero after PCR 4 extend")
	}

	// RTMR[2] should be non-zero (PCR 9 event mapped to it).
	if rtmrValues["2"].Expected == zeros96 {
		t.Error("RTMR[2] should not be zero after PCR 9 extend")
	}

	// RTMR[0] should be zero (no PCR 1 or 7 events).
	if rtmrValues["0"].Expected != zeros96 {
		t.Error("RTMR[0] should be zero with no PCR 1/7 events")
	}

	// RTMR[3] should be zero (no PCR maps to it).
	if rtmrValues["3"].Expected != zeros96 {
		t.Error("RTMR[3] should be zero (reserved)")
	}

	// Verify RTMR hex strings are 96 characters (48 bytes).
	for idx, val := range rtmrValues {
		if len(val.Expected) != 96 {
			t.Errorf("RTMR[%s] hex length: got %d, want 96", idx, len(val.Expected))
		}
	}
}
