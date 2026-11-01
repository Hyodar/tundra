package simulator

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tundraissuer "github.com/Hyodar/tundra/tools/pkg/attestation/issuer"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
)

type nopLogger struct{}

func (nopLogger) Debug(msg string, args ...any) {}
func (nopLogger) Info(msg string, args ...any)  {}
func (nopLogger) Warn(msg string, args ...any)  {}
func (nopLogger) Error(msg string, args ...any) {}

func TestSimulatorValidator_RoundTrip(t *testing.T) {
	// Issue a document using the tools simulator issuer
	simIssuer := tundraissuer.NewSimulatorIssuer()

	userData := []byte("test-user-data")
	nonce := []byte("test-nonce")

	doc, err := simIssuer.Issue(context.Background(), userData, nonce)
	if err != nil {
		t.Fatalf("issuer.Issue failed: %v", err)
	}

	docBytes, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}

	// Validate using the tdxs simulator validator
	validator := NewSimulatorValidator(nopLogger{})

	req := &api.ValidateRequest{
		Document: docBytes,
		Nonce:    nonce,
	}

	resp := validator.Validate(context.Background(), req)
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}
	if !resp.Valid {
		t.Fatal("expected valid response")
	}
	if string(resp.UserData) != "test-user-data" {
		t.Errorf("expected user data 'test-user-data', got %q", string(resp.UserData))
	}
}

func TestSimulatorValidator_NilDocument(t *testing.T) {
	validator := NewSimulatorValidator(nopLogger{})

	resp := validator.Validate(context.Background(), &api.ValidateRequest{
		Document: nil,
		Nonce:    []byte("nonce"),
	})
	if resp.Error == nil {
		t.Fatal("expected error for nil document")
	}
}

func TestSimulatorValidator_InvalidJSON(t *testing.T) {
	validator := NewSimulatorValidator(nopLogger{})

	resp := validator.Validate(context.Background(), &api.ValidateRequest{
		Document: []byte("not-json"),
		Nonce:    []byte("nonce"),
	})
	if resp.Error == nil {
		t.Fatal("expected error for invalid JSON")
	}
}

func TestSimulatorValidator_WrongNonce(t *testing.T) {
	simIssuer := tundraissuer.NewSimulatorIssuer()

	userData := []byte("test-user-data")
	nonce := []byte("correct-nonce")

	doc, err := simIssuer.Issue(context.Background(), userData, nonce)
	if err != nil {
		t.Fatalf("issuer.Issue failed: %v", err)
	}

	docBytes, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}

	validator := NewSimulatorValidator(nopLogger{})

	resp := validator.Validate(context.Background(), &api.ValidateRequest{
		Document: docBytes,
		Nonce:    []byte("wrong-nonce"),
	})
	if resp.Error == nil {
		t.Fatal("expected error for wrong nonce")
	}
	if resp.Valid {
		t.Fatal("expected invalid response for wrong nonce")
	}
}

func TestSimulatorValidator_WrongPlatform(t *testing.T) {
	doc := &attestation.AttestationDocument{
		RawQuote: make([]byte, 64),
		UserData: []byte("test"),
		Nonce:    []byte("nonce"),
		Platform: "wrong-platform",
	}

	docBytes, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}

	validator := NewSimulatorValidator(nopLogger{})

	resp := validator.Validate(context.Background(), &api.ValidateRequest{
		Document: docBytes,
		Nonce:    []byte("nonce"),
	})
	if resp.Error == nil {
		t.Fatal("expected error for wrong platform")
	}
}

func TestSimulatorValidator_Start(t *testing.T) {
	validator := NewSimulatorValidator(nopLogger{})
	if err := validator.Start(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}
