package measure

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"testing"
)

func TestAuthentihashBasic(t *testing.T) {
	// Build a minimal PE64 file and compute its Authentihash.
	sections := map[string][]byte{
		".text": []byte("hello world this is some code content for testing"),
	}
	peData := buildMinimalPE64(t, sections)

	r := bytes.NewReader(peData)
	digest, err := Authentihash(r)
	if err != nil {
		t.Fatalf("Authentihash error: %v", err)
	}

	// The digest should not be zero.
	if digest == [32]byte{} {
		t.Error("Authentihash returned all-zero digest")
	}
}

func TestAuthentihashDeterministic(t *testing.T) {
	// Same input should produce same output.
	sections := map[string][]byte{
		".text": []byte("deterministic content"),
		".data": []byte("some data section"),
	}
	peData := buildMinimalPE64(t, sections)

	r1 := bytes.NewReader(peData)
	d1, err := Authentihash(r1)
	if err != nil {
		t.Fatalf("first Authentihash error: %v", err)
	}

	r2 := bytes.NewReader(peData)
	d2, err := Authentihash(r2)
	if err != nil {
		t.Fatalf("second Authentihash error: %v", err)
	}

	if d1 != d2 {
		t.Errorf("Authentihash not deterministic: %x != %x", d1, d2)
	}
}

func TestAuthentihashDifferentContent(t *testing.T) {
	// Different content should produce different hashes.
	sections1 := map[string][]byte{
		".text": []byte("content A"),
	}
	sections2 := map[string][]byte{
		".text": []byte("content B"),
	}

	pe1 := buildMinimalPE64(t, sections1)
	pe2 := buildMinimalPE64(t, sections2)

	r1 := bytes.NewReader(pe1)
	d1, err := Authentihash(r1)
	if err != nil {
		t.Fatalf("Authentihash error: %v", err)
	}

	r2 := bytes.NewReader(pe2)
	d2, err := Authentihash(r2)
	if err != nil {
		t.Fatalf("Authentihash error: %v", err)
	}

	if d1 == d2 {
		t.Error("different PE content should produce different Authentihash")
	}
}

func TestAuthentihashExcludesChecksum(t *testing.T) {
	// Modifying only the checksum field should not change the Authentihash.
	sections := map[string][]byte{
		".text": []byte("checksum test content"),
	}
	peData := buildMinimalPE64(t, sections)

	r1 := bytes.NewReader(peData)
	d1, err := Authentihash(r1)
	if err != nil {
		t.Fatalf("Authentihash error: %v", err)
	}

	// Find the checksum offset and modify it.
	peOffset := binary.LittleEndian.Uint32(peData[0x3C:0x40])
	checksumOff := int(peOffset) + 4 + 20 + 64 // PE sig + COFF header + 64 bytes into opt header

	modified := make([]byte, len(peData))
	copy(modified, peData)
	binary.LittleEndian.PutUint32(modified[checksumOff:checksumOff+4], 0xDEADBEEF)

	r2 := bytes.NewReader(modified)
	d2, err := Authentihash(r2)
	if err != nil {
		t.Fatalf("Authentihash error after checksum modification: %v", err)
	}

	if d1 != d2 {
		t.Errorf("Authentihash should be unaffected by CheckSum field change: %x != %x", d1, d2)
	}
}

func TestAuthentihashInvalidInput(t *testing.T) {
	// Too-small input.
	r := bytes.NewReader([]byte("short"))
	_, err := Authentihash(r)
	if err == nil {
		t.Error("expected error for too-small input")
	}

	// Bad PE signature.
	bad := make([]byte, 256)
	binary.LittleEndian.PutUint32(bad[0x3C:0x40], 64)
	copy(bad[64:68], []byte("NOPE"))
	r2 := bytes.NewReader(bad)
	_, err = Authentihash(r2)
	if err == nil {
		t.Error("expected error for invalid PE signature")
	}
}

func TestAuthentihashKnownVector(t *testing.T) {
	// Build a specific PE and verify the hash can be manually replicated.
	// This acts as a regression test: we compute the hash once, record it,
	// and verify subsequent runs produce the same result.
	sections := map[string][]byte{
		".text": bytes.Repeat([]byte{0xCC}, 64),
	}
	peData := buildMinimalPE64(t, sections)

	r := bytes.NewReader(peData)
	digest, err := Authentihash(r)
	if err != nil {
		t.Fatalf("Authentihash error: %v", err)
	}

	// Manually compute what the hash should be by excluding the checksum
	// and certificate table directory entry.
	peOffset := int(binary.LittleEndian.Uint32(peData[0x3C:0x40]))
	optStart := peOffset + 4 + 20
	checksumOff := optStart + 64
	// Certificate table entry: at optStart + 112 + 4*8 = optStart + 144
	certEntryOff := optStart + 112 + 4*8

	h := sha256.New()
	// Write up to checksum.
	h.Write(peData[:checksumOff])
	// Skip 4 bytes (checksum), write up to cert table entry.
	h.Write(peData[checksumOff+4 : certEntryOff])
	// Skip 8 bytes (cert table entry), write rest.
	h.Write(peData[certEntryOff+8:])

	var expected [32]byte
	copy(expected[:], h.Sum(nil))

	if digest != expected {
		t.Errorf("Authentihash mismatch:\n  got:  %x\n  want: %x", digest, expected)
	}
}
