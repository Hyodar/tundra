package verifier

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	"github.com/Hyodar/tundra/tools/pkg/attestation/issuer"
)

func TestSimulatorVerifier_Platform(t *testing.T) {
	v := NewSimulatorVerifier()
	if got := v.Platform(); got != "simulator" {
		t.Errorf("Platform() = %q, want %q", got, "simulator")
	}
}

func TestSimulatorVerifier_VerifyValid(t *testing.T) {
	iss := issuer.NewSimulatorIssuer()
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	userData := []byte("test-user-data")
	nonce := []byte("test-nonce")

	doc, err := iss.Issue(ctx, userData, nonce)
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}

	result, err := ver.Verify(ctx, doc, nonce)
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if !result.Valid {
		t.Errorf("Verify() valid = false, want true; error: %v", result.Error)
	}
	if string(result.UserData) != "test-user-data" {
		t.Errorf("UserData = %q, want %q", result.UserData, "test-user-data")
	}
}

func TestSimulatorVerifier_VerifyEmptyInputs(t *testing.T) {
	iss := issuer.NewSimulatorIssuer()
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	doc, err := iss.Issue(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}

	result, err := ver.Verify(ctx, doc, nil)
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if !result.Valid {
		t.Errorf("Verify() valid = false, want true; error: %v", result.Error)
	}
}

func TestSimulatorVerifier_VerifyNilDoc(t *testing.T) {
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	result, err := ver.Verify(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify(nil) should not be valid")
	}
}

func TestSimulatorVerifier_VerifyWrongPlatform(t *testing.T) {
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	doc := &attestation.AttestationDocument{
		Platform: "tdx",
		RawQuote: make([]byte, 64),
	}

	result, err := ver.Verify(ctx, doc, nil)
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with wrong platform should not be valid")
	}
}

func TestSimulatorVerifier_VerifyWrongNonce(t *testing.T) {
	iss := issuer.NewSimulatorIssuer()
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	userData := []byte("data")
	nonce := []byte("correct-nonce")

	doc, err := iss.Issue(ctx, userData, nonce)
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}

	// Verify with a different nonce.
	result, err := ver.Verify(ctx, doc, []byte("wrong-nonce"))
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with wrong nonce should not be valid")
	}
}

func TestSimulatorVerifier_VerifyTamperedQuote(t *testing.T) {
	iss := issuer.NewSimulatorIssuer()
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	doc, err := iss.Issue(ctx, []byte("data"), []byte("nonce"))
	if err != nil {
		t.Fatalf("Issue() error: %v", err)
	}

	// Tamper with the HMAC tag.
	doc.RawQuote[63] ^= 0xff

	result, err := ver.Verify(ctx, doc, []byte("nonce"))
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with tampered quote should not be valid")
	}
}

func TestSimulatorVerifier_VerifyWrongQuoteLength(t *testing.T) {
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	doc := &attestation.AttestationDocument{
		Platform: "simulator",
		RawQuote: make([]byte, 32), // too short
	}

	result, err := ver.Verify(ctx, doc, nil)
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with wrong quote length should not be valid")
	}
}

func TestSimulatorVerifier_VerifyTamperedReportData(t *testing.T) {
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	// Build a quote with valid HMAC but for different data.
	h := sha256.New()
	h.Write([]byte("other-data"))
	h.Write([]byte("other-nonce"))
	otherHash := h.Sum(nil)

	mac := hmac.New(sha256.New, simulatorSecret)
	mac.Write(otherHash)
	tag := mac.Sum(nil)

	rawQuote := append(otherHash, tag...)

	doc := &attestation.AttestationDocument{
		Platform: "simulator",
		RawQuote: rawQuote,
		UserData: []byte("original-data"),
		Nonce:    []byte("original-nonce"),
	}

	result, err := ver.Verify(ctx, doc, []byte("original-nonce"))
	if err != nil {
		t.Fatalf("Verify() error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() should detect report data mismatch from tampered userData")
	}
}

func TestSimulatorVerifier_ImplementsInterface(t *testing.T) {
	var _ Verifier = NewSimulatorVerifier()
}

func TestSimulatorEndToEnd(t *testing.T) {
	// Full round-trip: issue then verify across multiple data combinations.
	tests := []struct {
		name     string
		userData []byte
		nonce    []byte
	}{
		{"both-set", []byte("user"), []byte("nonce")},
		{"empty-nonce", []byte("user"), nil},
		{"empty-userdata", nil, []byte("nonce")},
		{"both-empty", nil, nil},
		{"large-data", make([]byte, 1024), make([]byte, 256)},
	}

	iss := issuer.NewSimulatorIssuer()
	ver := NewSimulatorVerifier()
	ctx := context.Background()

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			doc, err := iss.Issue(ctx, tc.userData, tc.nonce)
			if err != nil {
				t.Fatalf("Issue() error: %v", err)
			}

			result, err := ver.Verify(ctx, doc, tc.nonce)
			if err != nil {
				t.Fatalf("Verify() error: %v", err)
			}
			if !result.Valid {
				t.Errorf("Verify() valid = false, want true; error: %v", result.Error)
			}
		})
	}
}
