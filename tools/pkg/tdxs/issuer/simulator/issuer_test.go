package simulator

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"

	"github.com/Hyodar/tundra/tools/pkg/tdxs/api"
)

type nopLogger struct{}

func (nopLogger) Debug(msg string, args ...any) {}
func (nopLogger) Info(msg string, args ...any)  {}
func (nopLogger) Warn(msg string, args ...any)  {}
func (nopLogger) Error(msg string, args ...any) {}

func TestSimulatorIssuer_Issue(t *testing.T) {
	issuer := NewSimulatorIssuer(nopLogger{})

	req := &api.IssueRequest{
		UserData: []byte("test-user-data"),
		Nonce:    []byte("test-nonce"),
	}

	resp := issuer.Issue(context.Background(), req)
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}
	if resp.Document == nil {
		t.Fatal("expected non-nil document")
	}

	// Verify the document can be unmarshaled into an AttestationDocument
	var doc attestation.AttestationDocument
	if err := json.Unmarshal(resp.Document, &doc); err != nil {
		t.Fatalf("failed to unmarshal document: %v", err)
	}

	if doc.Platform != "simulator" {
		t.Errorf("expected platform 'simulator', got %q", doc.Platform)
	}
	if string(doc.UserData) != "test-user-data" {
		t.Errorf("expected user data 'test-user-data', got %q", string(doc.UserData))
	}
	if string(doc.Nonce) != "test-nonce" {
		t.Errorf("expected nonce 'test-nonce', got %q", string(doc.Nonce))
	}
	if len(doc.RawQuote) != 64 {
		t.Errorf("expected raw quote length 64, got %d", len(doc.RawQuote))
	}
}

func TestSimulatorIssuer_Metadata(t *testing.T) {
	issuer := NewSimulatorIssuer(nopLogger{})

	resp := issuer.Metadata(context.Background(), &api.MetadataRequest{})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}
	if resp.IssuerType != "simulator" {
		t.Errorf("expected issuer type 'simulator', got %q", resp.IssuerType)
	}
	if resp.Metadata == nil {
		t.Fatal("expected non-nil metadata")
	}

	measurements, ok := resp.Metadata.(*attestation.TDXMeasurements)
	if !ok {
		t.Fatalf("expected *attestation.TDXMeasurements, got %T", resp.Metadata)
	}
	if measurements.MrTd == "" {
		t.Error("expected non-empty MrTd")
	}
}

func TestSimulatorIssuer_Start(t *testing.T) {
	issuer := NewSimulatorIssuer(nopLogger{})
	if err := issuer.Start(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}
