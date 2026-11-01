package issuer

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tdxabi "github.com/google/go-tdx-guest/abi"
	tdxclient "github.com/google/go-tdx-guest/client"
	pb "github.com/google/go-tdx-guest/proto/tdx"
)

const (
	platformAzureTDX = "azure-tdx"

	// Azure Instance Metadata Service (IMDS) endpoint for attestation token.
	azureIMDSAttestationURL = "http://169.254.169.254/metadata/attested/document?api-version=2021-02-01"
)

// AzureIMDSResponse represents a subset of the Azure IMDS attestation response.
type AzureIMDSResponse struct {
	Encoding  string `json:"encoding"`
	Signature string `json:"signature"`
}

// AzureAttestationExtra is serialized as JSON into AttestationDocument.Extra.
type AzureAttestationExtra struct {
	IMDSAttestation json.RawMessage `json:"imds_attestation,omitempty"`
}

// AzureTDXIssuer produces Azure TDX attestation documents that combine a
// TDX quote with Azure Instance Metadata Service (IMDS) attestation data.
type AzureTDXIssuer struct {
	provider   QuoteProvider
	httpClient *http.Client
	imdsURL    string
}

// AzureTDXIssuerOption configures AzureTDXIssuer construction.
type AzureTDXIssuerOption func(*AzureTDXIssuer)

// WithAzureQuoteProvider overrides the TDX quote provider for testing.
func WithAzureQuoteProvider(p QuoteProvider) AzureTDXIssuerOption {
	return func(i *AzureTDXIssuer) {
		i.provider = p
	}
}

// WithAzureHTTPClient overrides the HTTP client used for IMDS requests.
func WithAzureHTTPClient(c *http.Client) AzureTDXIssuerOption {
	return func(i *AzureTDXIssuer) {
		i.httpClient = c
	}
}

// WithAzureIMDSURL overrides the IMDS URL, primarily for testing.
func WithAzureIMDSURL(url string) AzureTDXIssuerOption {
	return func(i *AzureTDXIssuer) {
		i.imdsURL = url
	}
}

// NewAzureTDXIssuer creates an Azure TDX issuer that combines a raw TDX quote
// with Azure's IMDS attestation token.
func NewAzureTDXIssuer(opts ...AzureTDXIssuerOption) (*AzureTDXIssuer, error) {
	issuer := &AzureTDXIssuer{
		httpClient: http.DefaultClient,
		imdsURL:    azureIMDSAttestationURL,
	}
	for _, o := range opts {
		o(issuer)
	}
	if issuer.provider == nil {
		p, err := tdxclient.GetQuoteProvider()
		if err != nil {
			return nil, fmt.Errorf("azure-tdx: failed to get quote provider: %w", err)
		}
		issuer.provider = p
	}
	return issuer, nil
}

// Platform returns "azure-tdx".
func (a *AzureTDXIssuer) Platform() string { return platformAzureTDX }

// Issue produces an AttestationDocument that contains both a TDX quote and
// the Azure IMDS attestation token in the Extra field.
func (a *AzureTDXIssuer) Issue(ctx context.Context, userData, nonce []byte) (*attestation.AttestationDocument, error) {
	reportData := buildReportData(userData, nonce)

	rawQuote, err := tdxclient.GetRawQuote(a.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("azure-tdx: failed to get raw quote: %w", err)
	}

	imdsData, err := a.fetchIMDSAttestation(ctx)
	if err != nil {
		return nil, fmt.Errorf("azure-tdx: failed to fetch IMDS attestation: %w", err)
	}

	extra, err := json.Marshal(AzureAttestationExtra{
		IMDSAttestation: imdsData,
	})
	if err != nil {
		return nil, fmt.Errorf("azure-tdx: failed to marshal extra data: %w", err)
	}

	return &attestation.AttestationDocument{
		RawQuote: rawQuote,
		UserData: userData,
		Nonce:    nonce,
		Platform: platformAzureTDX,
		Extra:    extra,
	}, nil
}

// Measurements extracts TDX measurements from a quote with zeroed report data.
func (a *AzureTDXIssuer) Measurements(_ context.Context) (*attestation.TDXMeasurements, error) {
	var reportData [64]byte
	rawQuote, err := tdxclient.GetRawQuote(a.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("azure-tdx: failed to get quote for measurements: %w", err)
	}

	quoteAny, err := tdxabi.QuoteToProto(rawQuote)
	if err != nil {
		return nil, fmt.Errorf("azure-tdx: failed to parse quote: %w", err)
	}

	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return nil, fmt.Errorf("azure-tdx: unexpected quote type %T", quoteAny)
	}

	return extractMeasurements(q4), nil
}

// fetchIMDSAttestation retrieves the attestation document from Azure's IMDS.
func (a *AzureTDXIssuer) fetchIMDSAttestation(ctx context.Context) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.imdsURL, nil)
	if err != nil {
		return nil, fmt.Errorf("creating IMDS request: %w", err)
	}
	req.Header.Set("Metadata", "true")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("IMDS request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("IMDS returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("reading IMDS response: %w", err)
	}

	return json.RawMessage(body), nil
}
