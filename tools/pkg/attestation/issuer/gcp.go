package issuer

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
	tdxabi "github.com/google/go-tdx-guest/abi"
	tdxclient "github.com/google/go-tdx-guest/client"
	pb "github.com/google/go-tdx-guest/proto/tdx"
)

const (
	platformGCPTDX = "gcp-tdx"

	// GCP metadata server URL for instance identity token.
	gcpMetadataIdentityURL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=tundra-attestation&format=full"
)

// GCPAttestationExtra is serialized as JSON into AttestationDocument.Extra.
type GCPAttestationExtra struct {
	InstanceIdentityToken string `json:"instance_identity_token,omitempty"`
}

// GCPTDXIssuer produces GCP TDX attestation documents that combine a TDX
// quote with a GCP instance identity token from the metadata server.
type GCPTDXIssuer struct {
	provider    QuoteProvider
	httpClient  *http.Client
	metadataURL string
}

// GCPTDXIssuerOption configures GCPTDXIssuer construction.
type GCPTDXIssuerOption func(*GCPTDXIssuer)

// WithGCPQuoteProvider overrides the TDX quote provider for testing.
func WithGCPQuoteProvider(p QuoteProvider) GCPTDXIssuerOption {
	return func(i *GCPTDXIssuer) {
		i.provider = p
	}
}

// WithGCPHTTPClient overrides the HTTP client used for metadata requests.
func WithGCPHTTPClient(c *http.Client) GCPTDXIssuerOption {
	return func(i *GCPTDXIssuer) {
		i.httpClient = c
	}
}

// WithGCPMetadataURL overrides the metadata URL, primarily for testing.
func WithGCPMetadataURL(url string) GCPTDXIssuerOption {
	return func(i *GCPTDXIssuer) {
		i.metadataURL = url
	}
}

// NewGCPTDXIssuer creates a GCP TDX issuer that combines a raw TDX quote
// with a GCP instance identity token.
func NewGCPTDXIssuer(opts ...GCPTDXIssuerOption) (*GCPTDXIssuer, error) {
	issuer := &GCPTDXIssuer{
		httpClient:  metadataClient,
		metadataURL: gcpMetadataIdentityURL,
	}
	for _, o := range opts {
		o(issuer)
	}
	if issuer.provider == nil {
		p, err := tdxclient.GetQuoteProvider()
		if err != nil {
			return nil, fmt.Errorf("gcp-tdx: failed to get quote provider: %w", err)
		}
		issuer.provider = p
	}
	return issuer, nil
}

// Platform returns "gcp-tdx".
func (g *GCPTDXIssuer) Platform() string { return platformGCPTDX }

// Issue produces an AttestationDocument that contains both a TDX quote and
// the GCP instance identity token in the Extra field.
func (g *GCPTDXIssuer) Issue(ctx context.Context, userData, nonce []byte) (*attestation.AttestationDocument, error) {
	reportData := buildReportData(userData, nonce)

	rawQuote, err := tdxclient.GetRawQuote(g.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("gcp-tdx: failed to get raw quote: %w", err)
	}

	token, err := g.fetchInstanceIdentityToken(ctx)
	if err != nil {
		return nil, fmt.Errorf("gcp-tdx: failed to fetch identity token: %w", err)
	}

	extra, err := json.Marshal(GCPAttestationExtra{
		InstanceIdentityToken: token,
	})
	if err != nil {
		return nil, fmt.Errorf("gcp-tdx: failed to marshal extra data: %w", err)
	}

	return &attestation.AttestationDocument{
		RawQuote: rawQuote,
		UserData: userData,
		Nonce:    nonce,
		Platform: platformGCPTDX,
		Extra:    extra,
	}, nil
}

// Measurements extracts TDX measurements from a quote with zeroed report data.
func (g *GCPTDXIssuer) Measurements(_ context.Context) (*attestation.TDXMeasurements, error) {
	var reportData [64]byte
	rawQuote, err := tdxclient.GetRawQuote(g.provider, reportData)
	if err != nil {
		return nil, fmt.Errorf("gcp-tdx: failed to get quote for measurements: %w", err)
	}

	quoteAny, err := tdxabi.QuoteToProto(rawQuote)
	if err != nil {
		return nil, fmt.Errorf("gcp-tdx: failed to parse quote: %w", err)
	}

	q4, ok := quoteAny.(*pb.QuoteV4)
	if !ok {
		return nil, fmt.Errorf("gcp-tdx: unexpected quote type %T", quoteAny)
	}

	return extractMeasurements(q4), nil
}

// fetchInstanceIdentityToken retrieves an identity token from the GCP metadata server.
func (g *GCPTDXIssuer) fetchInstanceIdentityToken(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, g.metadataURL, nil)
	if err != nil {
		return "", fmt.Errorf("creating metadata request: %w", err)
	}
	req.Header.Set("Metadata-Flavor", "Google")

	resp, err := g.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("metadata request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("metadata server returned status %d", resp.StatusCode)
	}

	body, err := readMetadata(resp.Body, "the metadata response")
	if err != nil {
		return "", err
	}

	return string(body), nil
}
