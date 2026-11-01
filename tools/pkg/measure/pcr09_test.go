package measure

import (
	"crypto/sha256"
	"encoding/binary"
	"testing"
)

func TestPredictPCR9(t *testing.T) {
	sim := NewSimulator()
	cmdline := []byte("root=/dev/sda1 console=ttyS0")
	initrd := []byte("fake initrd content for testing")

	if err := PredictPCR9(sim, cmdline, initrd); err != nil {
		t.Fatalf("PredictPCR9 error: %v", err)
	}

	// PCR 9 should not be zero.
	if sim.Bank[9] == [32]byte{} {
		t.Error("PCR 9 is still zero after PredictPCR9")
	}

	// Should have 2 events.
	if len(sim.EventLog.Events) != 2 {
		t.Errorf("expected 2 events, got %d", len(sim.EventLog.Events))
	}
}

func TestPredictPCR9Deterministic(t *testing.T) {
	cmdline := []byte("root=/dev/sda1")
	initrd := []byte("initrd data")

	sim1 := NewSimulator()
	if err := PredictPCR9(sim1, cmdline, initrd); err != nil {
		t.Fatal(err)
	}

	sim2 := NewSimulator()
	if err := PredictPCR9(sim2, cmdline, initrd); err != nil {
		t.Fatal(err)
	}

	if sim1.Bank[9] != sim2.Bank[9] {
		t.Errorf("PredictPCR9 not deterministic: %x != %x", sim1.Bank[9], sim2.Bank[9])
	}
}

func TestUTF8ToUTF16LE(t *testing.T) {
	tests := []struct {
		name     string
		input    []byte
		expected []byte
	}{
		{
			name:  "simple ASCII",
			input: []byte("AB"),
			expected: []byte{
				'A', 0x00, // 'A' in UTF-16LE
				'B', 0x00, // 'B' in UTF-16LE
			},
		},
		{
			name:     "empty",
			input:    []byte{},
			expected: []byte{},
		},
		{
			name:  "single char",
			input: []byte("x"),
			expected: []byte{
				'x', 0x00,
			},
		},
		{
			name:  "null byte",
			input: []byte{0x00},
			expected: []byte{
				0x00, 0x00,
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := utf8ToUTF16LE(tc.input)
			if err != nil {
				t.Fatalf("utf8ToUTF16LE error: %v", err)
			}
			if len(got) != len(tc.expected) {
				t.Fatalf("length mismatch: got %d, want %d", len(got), len(tc.expected))
			}
			for i := range got {
				if got[i] != tc.expected[i] {
					t.Errorf("byte %d: got 0x%02x, want 0x%02x", i, got[i], tc.expected[i])
				}
			}
		})
	}
}

func TestUTF8ToUTF16LECommandLine(t *testing.T) {
	// Test with a typical kernel command line.
	cmdline := []byte("root=/dev/sda1 console=ttyS0 quiet")
	utf16, err := utf8ToUTF16LE(cmdline)
	if err != nil {
		t.Fatal(err)
	}

	// Each ASCII character should become 2 bytes in UTF-16LE.
	if len(utf16) != len(cmdline)*2 {
		t.Errorf("UTF-16LE length: got %d, want %d", len(utf16), len(cmdline)*2)
	}

	// Verify some characters.
	for i, ch := range cmdline {
		lo := utf16[i*2]
		hi := utf16[i*2+1]
		if lo != ch || hi != 0 {
			t.Errorf("char %d (%c): got [0x%02x, 0x%02x], want [0x%02x, 0x00]",
				i, ch, lo, hi, ch)
		}
	}
}

func TestPredictPCR9ManualVerification(t *testing.T) {
	// Manually compute the expected PCR 9 value for a known input.
	cmdline := []byte("test")
	initrd := []byte("data")

	// Step 1: Convert cmdline to UTF-16LE.
	utf16Cmdline := make([]byte, len(cmdline)*2)
	for i, b := range cmdline {
		binary.LittleEndian.PutUint16(utf16Cmdline[i*2:i*2+2], uint16(b))
	}
	cmdlineDigest := sha256.Sum256(utf16Cmdline)

	// Step 2: Hash initrd.
	initrdDigest := sha256.Sum256(initrd)

	// Step 3: Compute expected PCR 9.
	var pcr9 [32]byte
	combined1 := append(pcr9[:], cmdlineDigest[:]...)
	pcr9 = sha256.Sum256(combined1)
	combined2 := append(pcr9[:], initrdDigest[:]...)
	pcr9 = sha256.Sum256(combined2)

	// Now verify with PredictPCR9.
	sim := NewSimulator()
	if err := PredictPCR9(sim, cmdline, initrd); err != nil {
		t.Fatal(err)
	}

	if sim.Bank[9] != pcr9 {
		t.Errorf("PCR 9 mismatch:\n  got:  %x\n  want: %x", sim.Bank[9], pcr9)
	}
}
