package issuer

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

// maxMetadata bounds what the issuers read from the cloud metadata server, the host's to
// answer: Azure's attested document and Compute Engine's identity token are a few KiB.
const maxMetadata = 64 << 10

// metadataClient is the issuers' default HTTP client: a metadata server that never
// answers holds a quote for ten seconds, not forever.
var metadataClient = &http.Client{Timeout: 10 * time.Second}

// readMetadata reads body whole, refusing one larger than maxMetadata.
func readMetadata(body io.Reader, what string) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(body, maxMetadata+1))
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", what, err)
	}
	if len(data) > maxMetadata {
		return nil, fmt.Errorf("%s is larger than %d bytes", what, maxMetadata)
	}
	return data, nil
}
