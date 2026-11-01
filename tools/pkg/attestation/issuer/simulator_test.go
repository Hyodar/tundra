package issuer

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

func TestSimulatorIssuer_Platform(t *testing.T) {
	s := NewSimulatorIssuer()
	if got := s.Platform(); got != "simulator" {
		t.Errorf("Platform() = %q, want %q", got, "simulator")
	}
}

func TestSimulatorIssuer_Issue(t *testing.T) {
	s := NewSimulatorIssuer()
	ctx := context.Background()
	userData := []byte("hello")
	nonce := []byte("world")

	doc, err := s.Issue(ctx, userData, nonce)
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}

	if doc.Platform != "simulator" {
		t.Errorf("Platform = %q, want %q", doc.Platform, "simulator")
	}
	if string(doc.UserData) != "hello" {
		t.Errorf("UserData = %q, want %q", doc.UserData, "hello")
	}
	if string(doc.Nonce) != "world" {
		t.Errorf("Nonce = %q, want %q", doc.Nonce, "world")
	}
	if len(doc.RawQuote) != 64 {
		t.Fatalf("RawQuote length = %d, want 64", len(doc.RawQuote))
	}

	// Verify the report data hash.
	h := sha256.New()
	h.Write(userData)
	h.Write(nonce)
	expectedHash := h.Sum(nil)

	if got := doc.RawQuote[:32]; !hmac.Equal(got, expectedHash) {
		t.Errorf("report data hash mismatch:\n  got:  %x\n  want: %x", got, expectedHash)
	}

	// Verify the HMAC tag.
	mac := hmac.New(sha256.New, simulatorSecret)
	mac.Write(expectedHash)
	expectedTag := mac.Sum(nil)

	if got := doc.RawQuote[32:]; !hmac.Equal(got, expectedTag) {
		t.Errorf("HMAC tag mismatch:\n  got:  %x\n  want: %x", got, expectedTag)
	}
}

func TestSimulatorIssuer_Issue_EmptyInputs(t *testing.T) {
	s := NewSimulatorIssuer()
	ctx := context.Background()

	doc, err := s.Issue(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}
	if len(doc.RawQuote) != 64 {
		t.Fatalf("RawQuote length = %d, want 64", len(doc.RawQuote))
	}
}

func TestSimulatorIssuer_Issue_Deterministic(t *testing.T) {
	s := NewSimulatorIssuer()
	ctx := context.Background()
	userData := []byte("data")
	nonce := []byte("nonce")

	doc1, _ := s.Issue(ctx, userData, nonce)
	doc2, _ := s.Issue(ctx, userData, nonce)

	if !hmac.Equal(doc1.RawQuote, doc2.RawQuote) {
		t.Error("same inputs should produce same quote")
	}
}

func TestSimulatorIssuer_Issue_DifferentInputs(t *testing.T) {
	s := NewSimulatorIssuer()
	ctx := context.Background()

	doc1, _ := s.Issue(ctx, []byte("a"), []byte("b"))
	doc2, _ := s.Issue(ctx, []byte("c"), []byte("d"))

	if hmac.Equal(doc1.RawQuote, doc2.RawQuote) {
		t.Error("different inputs should produce different quotes")
	}
}

func TestSimulatorIssuer_Measurements(t *testing.T) {
	s := NewSimulatorIssuer()
	ctx := context.Background()

	m, err := s.Measurements(ctx)
	if err != nil {
		t.Fatalf("Measurements() error: %v", err)
	}

	// Default measurements should be all zeros.
	zeroHash48 := hex.EncodeToString(make([]byte, 48))
	zeroHash8 := hex.EncodeToString(make([]byte, 8))

	if m.MrTd != zeroHash48 {
		t.Errorf("MrTd = %q, want %q", m.MrTd, zeroHash48)
	}
	if m.XFAM != zeroHash8 {
		t.Errorf("XFAM = %q, want %q", m.XFAM, zeroHash8)
	}
}

func TestSimulatorIssuer_CustomMeasurements(t *testing.T) {
	custom := &attestation.TDXMeasurements{
		MrTd:  "aabbccdd",
		Rtmr0: "11223344",
	}
	s := NewSimulatorIssuer(WithSimulatorMeasurements(custom))
	ctx := context.Background()

	m, err := s.Measurements(ctx)
	if err != nil {
		t.Fatalf("Measurements() error: %v", err)
	}
	if m.MrTd != "aabbccdd" {
		t.Errorf("MrTd = %q, want %q", m.MrTd, "aabbccdd")
	}
	if m.Rtmr0 != "11223344" {
		t.Errorf("Rtmr0 = %q, want %q", m.Rtmr0, "11223344")
	}
}

func TestSimulatorIssuer_ImplementsInterface(t *testing.T) {
	var _ Issuer = NewSimulatorIssuer()
}
