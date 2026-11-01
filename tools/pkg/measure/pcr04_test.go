package measure

import (
	"crypto/sha256"
	"testing"
)

func TestPredictPCR4(t *testing.T) {
	// Create three minimal PE files to represent sd-boot, UKI, and linux kernel.
	sdBootPath := writeTempPE(t, map[string][]byte{
		".text": []byte("sd-boot code"),
	})
	ukiPath := writeTempPE(t, map[string][]byte{
		".text":    []byte("UKI stub code"),
		".linux":   []byte("embedded linux kernel"),
		".cmdline": []byte("root=/dev/sda1"),
		".initrd":  []byte("fake initrd"),
	})
	linuxPath := writeTempPE(t, map[string][]byte{
		".text": []byte("linux kernel code"),
	})

	sim := NewSimulator()
	if err := PredictPCR4(sim, sdBootPath, ukiPath, linuxPath); err != nil {
		t.Fatalf("PredictPCR4 error: %v", err)
	}

	// PCR 4 should not be zero after prediction.
	if sim.Bank[4] == [32]byte{} {
		t.Error("PCR 4 is still zero after PredictPCR4")
	}

	// Should have 5 events: EV_EFI_ACTION, EV_SEPARATOR, sd-boot, UKI, linux.
	if len(sim.EventLog.Events) != 5 {
		t.Errorf("expected 5 events, got %d", len(sim.EventLog.Events))
	}

	// Verify event types.
	expectedTypes := []string{
		"EV_EFI_ACTION",
		"EV_SEPARATOR",
		"EV_EFI_BOOT_SERVICES_APPLICATION",
		"EV_EFI_BOOT_SERVICES_APPLICATION",
		"EV_EFI_BOOT_SERVICES_APPLICATION",
	}
	for i, et := range expectedTypes {
		if i >= len(sim.EventLog.Events) {
			break
		}
		if sim.EventLog.Events[i].Type != et {
			t.Errorf("event %d type: got %q, want %q", i, sim.EventLog.Events[i].Type, et)
		}
	}
}

func TestEVEFIAction(t *testing.T) {
	expected := sha256.Sum256([]byte("Calling EFI Application from Boot Option"))
	if EVEFIAction != expected {
		t.Errorf("EVEFIAction mismatch: got %x, want %x", EVEFIAction, expected)
	}
}

func TestEVSeparator(t *testing.T) {
	expected := sha256.Sum256([]byte{0x00, 0x00, 0x00, 0x00})
	if EVSeparator != expected {
		t.Errorf("EVSeparator mismatch: got %x, want %x", EVSeparator, expected)
	}
}

func TestPredictPCR4Deterministic(t *testing.T) {
	sdBootPath := writeTempPE(t, map[string][]byte{
		".text": []byte("sd-boot code v2"),
	})
	ukiPath := writeTempPE(t, map[string][]byte{
		".text": []byte("UKI code v2"),
	})
	linuxPath := writeTempPE(t, map[string][]byte{
		".text": []byte("linux code v2"),
	})

	sim1 := NewSimulator()
	if err := PredictPCR4(sim1, sdBootPath, ukiPath, linuxPath); err != nil {
		t.Fatalf("first PredictPCR4 error: %v", err)
	}

	sim2 := NewSimulator()
	if err := PredictPCR4(sim2, sdBootPath, ukiPath, linuxPath); err != nil {
		t.Fatalf("second PredictPCR4 error: %v", err)
	}

	if sim1.Bank[4] != sim2.Bank[4] {
		t.Errorf("PredictPCR4 not deterministic: %x != %x", sim1.Bank[4], sim2.Bank[4])
	}
}
