package measure

import (
	"crypto/sha256"
	"fmt"
)

// PredictPCR11 predicts the value of PCR 11, which records UKI
// (Unified Kernel Image) section measurements. systemd-stub measures
// each relevant section by first extending the null-terminated section
// name, then extending the section content digest.
func PredictPCR11(sim *Simulator, sections []PESection) error {
	for _, sec := range sections {
		if !sec.Measure {
			continue
		}

		// Step 1: Extend with the SHA-256 hash of the null-terminated section name.
		nameDigest := sha256.Sum256(NullTerminatedName(sec.Name))
		sim.Extend(11, nameDigest, "EV_IPL",
			fmt.Sprintf("section name: %s", sec.Name))

		// Step 2: Extend with the section content digest.
		sim.Extend(11, sec.Digest, "EV_IPL",
			fmt.Sprintf("section data: %s", sec.Name))
	}
	return nil
}
