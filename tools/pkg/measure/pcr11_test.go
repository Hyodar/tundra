package measure

import (
	"crypto/sha256"
	"testing"
)

func TestPredictPCR11(t *testing.T) {
	sections := []PESection{
		{Name: ".linux", Digest: sha256.Sum256([]byte("linux kernel")), Measure: true, MeasureOrder: 0},
		{Name: ".osrel", Digest: sha256.Sum256([]byte("os-release")), Measure: true, MeasureOrder: 1},
		{Name: ".cmdline", Digest: sha256.Sum256([]byte("root=/dev/sda1")), Measure: true, MeasureOrder: 2},
		{Name: ".text", Digest: sha256.Sum256([]byte("stub code")), Measure: false},
	}

	sim := NewSimulator()
	if err := PredictPCR11(sim, sections); err != nil {
		t.Fatalf("PredictPCR11 error: %v", err)
	}

	// PCR 11 should not be zero (3 measured sections = 6 extends).
	if sim.Bank[11] == [32]byte{} {
		t.Error("PCR 11 is still zero after PredictPCR11")
	}

	// Should have 6 events (2 per measured section: name + data).
	if len(sim.EventLog.Events) != 6 {
		t.Errorf("expected 6 events, got %d", len(sim.EventLog.Events))
	}
}

func TestPredictPCR11NoMeasuredSections(t *testing.T) {
	sections := []PESection{
		{Name: ".text", Digest: sha256.Sum256([]byte("code")), Measure: false},
		{Name: ".data", Digest: sha256.Sum256([]byte("data")), Measure: false},
	}

	sim := NewSimulator()
	if err := PredictPCR11(sim, sections); err != nil {
		t.Fatalf("PredictPCR11 error: %v", err)
	}

	// PCR 11 should remain zero.
	if sim.Bank[11] != [32]byte{} {
		t.Error("PCR 11 should be zero when no sections are measured")
	}

	// No events.
	if len(sim.EventLog.Events) != 0 {
		t.Errorf("expected 0 events, got %d", len(sim.EventLog.Events))
	}
}

func TestPredictPCR11ManualVerification(t *testing.T) {
	// Manually compute expected PCR 11 for a single section.
	sectionData := []byte("hello kernel")
	sectionDigest := sha256.Sum256(sectionData)

	sections := []PESection{
		{Name: ".linux", Digest: sectionDigest, Measure: true, MeasureOrder: 0},
	}

	// Manual computation:
	// Step 1: Extend with SHA256(NullTerminatedName(".linux"))
	nameDigest := sha256.Sum256(NullTerminatedName(".linux"))
	var pcr11 [32]byte
	combined1 := append(pcr11[:], nameDigest[:]...)
	pcr11 = sha256.Sum256(combined1)

	// Step 2: Extend with section digest.
	combined2 := append(pcr11[:], sectionDigest[:]...)
	pcr11 = sha256.Sum256(combined2)

	sim := NewSimulator()
	if err := PredictPCR11(sim, sections); err != nil {
		t.Fatal(err)
	}

	if sim.Bank[11] != pcr11 {
		t.Errorf("PCR 11 mismatch:\n  got:  %x\n  want: %x", sim.Bank[11], pcr11)
	}
}

func TestPredictPCR11Deterministic(t *testing.T) {
	sections := []PESection{
		{Name: ".linux", Digest: sha256.Sum256([]byte("kernel")), Measure: true, MeasureOrder: 0},
		{Name: ".initrd", Digest: sha256.Sum256([]byte("initrd")), Measure: true, MeasureOrder: 3},
	}

	sim1 := NewSimulator()
	if err := PredictPCR11(sim1, sections); err != nil {
		t.Fatal(err)
	}

	sim2 := NewSimulator()
	if err := PredictPCR11(sim2, sections); err != nil {
		t.Fatal(err)
	}

	if sim1.Bank[11] != sim2.Bank[11] {
		t.Errorf("PredictPCR11 not deterministic: %x != %x", sim1.Bank[11], sim2.Bank[11])
	}
}

func TestPredictPCR11OrderMatters(t *testing.T) {
	digest1 := sha256.Sum256([]byte("first"))
	digest2 := sha256.Sum256([]byte("second"))

	// Order A: .linux then .osrel
	sectionsA := []PESection{
		{Name: ".linux", Digest: digest1, Measure: true, MeasureOrder: 0},
		{Name: ".osrel", Digest: digest2, Measure: true, MeasureOrder: 1},
	}

	// Order B: .osrel then .linux (reversed)
	sectionsB := []PESection{
		{Name: ".osrel", Digest: digest2, Measure: true, MeasureOrder: 0},
		{Name: ".linux", Digest: digest1, Measure: true, MeasureOrder: 1},
	}

	simA := NewSimulator()
	if err := PredictPCR11(simA, sectionsA); err != nil {
		t.Fatal(err)
	}

	simB := NewSimulator()
	if err := PredictPCR11(simB, sectionsB); err != nil {
		t.Fatal(err)
	}

	// Different order should produce different results.
	if simA.Bank[11] == simB.Bank[11] {
		t.Error("different measurement order should produce different PCR 11 values")
	}
}

func TestPredictPCR11WithPESections(t *testing.T) {
	// Integration test using actual PE file parsing.
	ukiPath := writeTempPE(t, map[string][]byte{
		".linux":   []byte("linux kernel image data"),
		".osrel":   []byte("PRETTY_NAME=\"Test OS\"\n"),
		".cmdline": []byte("root=/dev/sda1 quiet"),
		".initrd":  []byte("compressed initramfs data"),
		".text":    []byte("stub code"),
		".pcrsig":  []byte("signature data that should not be measured"),
	})

	sections, err := PeSectionDigests(ukiPath)
	if err != nil {
		t.Fatalf("PeSectionDigests error: %v", err)
	}

	sim := NewSimulator()
	if err := PredictPCR11(sim, sections); err != nil {
		t.Fatalf("PredictPCR11 error: %v", err)
	}

	// PCR 11 should not be zero (we have measured sections).
	if sim.Bank[11] == [32]byte{} {
		t.Error("PCR 11 should not be zero")
	}

	// Count measured events: should be 4 sections * 2 events each = 8.
	// (.linux, .osrel, .cmdline, .initrd are measured; .text and .pcrsig are not)
	if len(sim.EventLog.Events) != 8 {
		t.Errorf("expected 8 events, got %d", len(sim.EventLog.Events))
		for i, ev := range sim.EventLog.Events {
			t.Logf("  event %d: PCR=%d type=%s desc=%s", i, ev.PCR, ev.Type, ev.Desc)
		}
	}
}
