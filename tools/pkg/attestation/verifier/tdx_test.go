package verifier

import (
	"context"
	"crypto/sha256"
	"strings"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	"github.com/google/go-tdx-guest/testing/testdata"
)

func TestTDXVerifier_Platform(t *testing.T) {
	v := NewTDXVerifier(TDXVerifyOptions{})
	if got := v.Platform(); got != "tdx" {
		t.Errorf("Platform() = %q, want %q", got, "tdx")
	}
}

func TestTDXVerifier_VerifyNilDoc(t *testing.T) {
	v := NewTDXVerifier(TDXVerifyOptions{})
	ctx := context.Background()

	result, err := v.Verify(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Verify() unexpected error: %v", err)
	}
	if result.Valid {
		t.Error("Verify(nil) should not be valid")
	}
	if result.Error == nil {
		t.Error("Verify(nil) should set Error")
	}
}

func TestTDXVerifier_VerifyWrongPlatform(t *testing.T) {
	v := NewTDXVerifier(TDXVerifyOptions{})
	ctx := context.Background()

	doc := &attestation.AttestationDocument{
		Platform: "azure-tdx",
	}

	result, err := v.Verify(ctx, doc, nil)
	if err != nil {
		t.Fatalf("Verify() unexpected error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with wrong platform should not be valid")
	}
}

func TestTDXVerifier_VerifyMalformedQuote(t *testing.T) {
	v := NewTDXVerifier(TDXVerifyOptions{})
	ctx := context.Background()

	doc := &attestation.AttestationDocument{
		Platform: "tdx",
		RawQuote: []byte("not-a-real-quote"),
	}

	result, err := v.Verify(ctx, doc, nil)
	if err != nil {
		t.Fatalf("Verify() unexpected error: %v", err)
	}
	if result.Valid {
		t.Error("Verify() with malformed quote should not be valid")
	}
	if result.Error == nil {
		t.Error("Verify() with malformed quote should set Error")
	}
}

func TestBuildExpectedReportData(t *testing.T) {
	tests := []struct {
		name     string
		userData []byte
		nonce    []byte
	}{
		{"both-set", []byte("user"), []byte("nonce")},
		{"empty-nonce", []byte("user"), nil},
		{"empty-userdata", nil, []byte("nonce")},
		{"both-empty", nil, nil},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := buildExpectedReportData(tc.userData, tc.nonce)

			h := sha256.New()
			h.Write(tc.userData)
			h.Write(tc.nonce)
			want := h.Sum(nil)

			for i := 0; i < 32; i++ {
				if got[i] != want[i] {
					t.Errorf("byte %d: got %02x, want %02x", i, got[i], want[i])
				}
			}
		})
	}
}

func TestCompareMeasurements(t *testing.T) {
	t.Run("nil-actual", func(t *testing.T) {
		err := compareMeasurements(nil, &attestation.TDXMeasurements{MrTd: "abc"})
		if err == nil {
			t.Error("compareMeasurements(nil, expected) should return error")
		}
	})

	t.Run("empty-expected-fields-pass", func(t *testing.T) {
		actual := &attestation.TDXMeasurements{
			MrTd:  "abc",
			Rtmr0: "def",
		}
		expected := &attestation.TDXMeasurements{} // all empty = no checks

		if err := compareMeasurements(actual, expected); err != nil {
			t.Errorf("compareMeasurements() error: %v", err)
		}
	})

	t.Run("matching-fields", func(t *testing.T) {
		m := &attestation.TDXMeasurements{
			MrTd:   "aabb",
			MrSeam: "ccdd",
			Rtmr0:  "eeff",
		}
		if err := compareMeasurements(m, m); err != nil {
			t.Errorf("compareMeasurements() should pass for identical measurements: %v", err)
		}
	})

	t.Run("partial-match", func(t *testing.T) {
		actual := &attestation.TDXMeasurements{
			MrTd:   "aabb",
			MrSeam: "ccdd",
			Rtmr0:  "eeff",
		}
		expected := &attestation.TDXMeasurements{
			MrTd: "aabb",
			// MrSeam and Rtmr0 not checked
		}
		if err := compareMeasurements(actual, expected); err != nil {
			t.Errorf("compareMeasurements() should pass for partial match: %v", err)
		}
	})

	t.Run("mismatch", func(t *testing.T) {
		actual := &attestation.TDXMeasurements{
			MrTd: "aabb",
		}
		expected := &attestation.TDXMeasurements{
			MrTd: "ccdd",
		}
		if err := compareMeasurements(actual, expected); err == nil {
			t.Error("compareMeasurements() should fail for mismatch")
		}
	})

	t.Run("all-fields-checked", func(t *testing.T) {
		m := &attestation.TDXMeasurements{
			MrTd:    "1",
			MrOwner: "2",
			MrSeam:  "3",
			Rtmr0:   "4",
			Rtmr1:   "5",
			Rtmr2:   "6",
			Rtmr3:   "7",
			XFAM:    "8",
		}
		if err := compareMeasurements(m, m); err != nil {
			t.Errorf("compareMeasurements() should pass when all fields match: %v", err)
		}

		wrongXFAM := *m
		wrongXFAM.XFAM = "9"
		if err := compareMeasurements(m, &wrongXFAM); err == nil {
			t.Error("compareMeasurements() should fail for XFAM mismatch")
		}
	})
}

func TestAzureTDXVerifier_Platform(t *testing.T) {
	v := NewAzureTDXVerifier(AzureTDXVerifyOptions{})
	if got := v.Platform(); got != "azure-tdx" {
		t.Errorf("Platform() = %q, want %q", got, "azure-tdx")
	}
}

func TestAzureTDXVerifier_VerifyNilDoc(t *testing.T) {
	v := NewAzureTDXVerifier(AzureTDXVerifyOptions{})
	ctx := context.Background()

	result, err := v.Verify(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Verify() unexpected error: %v", err)
	}
	if result.Valid {
		t.Error("Verify(nil) should not be valid")
	}
}

func TestGCPTDXVerifier_Platform(t *testing.T) {
	v := NewGCPTDXVerifier(GCPTDXVerifyOptions{})
	if got := v.Platform(); got != "gcp-tdx" {
		t.Errorf("Platform() = %q, want %q", got, "gcp-tdx")
	}
}

func TestGCPTDXVerifier_VerifyNilDoc(t *testing.T) {
	v := NewGCPTDXVerifier(GCPTDXVerifyOptions{})
	ctx := context.Background()

	result, err := v.Verify(ctx, nil, nil)
	if err != nil {
		t.Fatalf("Verify() unexpected error: %v", err)
	}
	if result.Valid {
		t.Error("Verify(nil) should not be valid")
	}
}

func TestFailResult(t *testing.T) {
	result := failResult(nil)
	if result.Valid {
		t.Error("failResult should have Valid=false")
	}

	err := result.Error
	if err != nil {
		t.Errorf("failResult(nil) should have nil Error, got %v", err)
	}
}

func TestTDXVerifier_ImplementsInterface(t *testing.T) {
	var _ Verifier = NewTDXVerifier(TDXVerifyOptions{})
}

func TestAzureTDXVerifier_ImplementsInterface(t *testing.T) {
	var _ Verifier = NewAzureTDXVerifier(AzureTDXVerifyOptions{})
}

func TestGCPTDXVerifier_ImplementsInterface(t *testing.T) {
	var _ Verifier = NewGCPTDXVerifier(GCPTDXVerifyOptions{})
}

// tdAttributesAt is where a DCAP v4 quote holds TD_ATTRIBUTES: after the 48-byte header,
// TEE_TCB_SVN (16), MRSEAM (48), MRSIGNERSEAM (48) and SEAM_ATTRIBUTES (8).
const tdAttributesAt = 48 + 16 + 48 + 48 + 8

func TestVerifiersRefuseADebugTD(t *testing.T) {
	debug := append([]byte{}, testdata.RawQuote...)
	debug[tdAttributesAt] |= tdDebug
	verifiers := []Verifier{
		NewTDXVerifier(TDXVerifyOptions{}),
		NewAzureTDXVerifier(AzureTDXVerifyOptions{}),
		NewGCPTDXVerifier(GCPTDXVerifyOptions{}),
	}
	cases := []struct {
		name    string
		quote   []byte
		refused bool
	}{
		{"a quote whose TD sets DEBUG", debug, true},
		{"a production TD's quote", testdata.RawQuote, false},
	}
	for _, v := range verifiers {
		for _, c := range cases {
			t.Run(v.Platform()+": "+c.name, func(t *testing.T) {
				doc := &attestation.AttestationDocument{Platform: v.Platform(), RawQuote: c.quote}
				result, err := v.Verify(context.Background(), doc, nil)
				if err != nil {
					t.Fatal(err)
				}
				refused := result.Error != nil && strings.Contains(result.Error.Error(), "TUD.DEBUG")
				if refused != c.refused || (c.refused && result.Valid) {
					t.Fatalf("valid %v, error %v; want a DEBUG refusal: %v", result.Valid, result.Error, c.refused)
				}
			})
		}
	}
}
