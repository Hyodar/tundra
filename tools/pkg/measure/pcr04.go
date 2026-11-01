package measure

import (
	"crypto/sha256"
	"fmt"
	"os"
)

// EVEFIAction is the digest of the EV_EFI_ACTION string that firmware
// extends into PCR 4 before launching the first boot application.
// Per TCG PC Client Platform Firmware Profile Specification.
var EVEFIAction = sha256.Sum256([]byte("Calling EFI Application from Boot Option"))

// EVSeparator is the digest of the EV_SEPARATOR event (4 null bytes)
// that the firmware extends into PCR 4 after the pre-OS phase.
var EVSeparator = sha256.Sum256([]byte{0x00, 0x00, 0x00, 0x00})

// PredictPCR4 predicts the value of PCR 4, which records the EFI boot
// chain measurements. The boot chain consists of:
//  1. An EV_EFI_ACTION event for boot option invocation
//  2. An EV_SEPARATOR marking end of pre-OS measurements
//  3. The Authenticode hash of the systemd-boot EFI binary
//  4. The Authenticode hash of the Unified Kernel Image
//  5. The Authenticode hash of the Linux kernel (from inside the UKI)
func PredictPCR4(sim *Simulator, sdBootPath, ukiPath, linuxPath string) error {
	// Step 1: EV_EFI_ACTION - "Calling EFI Application from Boot Option"
	sim.Extend(4, EVEFIAction, "EV_EFI_ACTION", "Calling EFI Application from Boot Option")

	// Step 2: EV_SEPARATOR
	sim.Extend(4, EVSeparator, "EV_SEPARATOR", "separator")

	// Step 3: Authenticode hash of sd-boot
	sdBootHash, err := authentihashFromPath(sdBootPath)
	if err != nil {
		return fmt.Errorf("computing sd-boot authentihash: %w", err)
	}
	sim.Extend(4, sdBootHash, "EV_EFI_BOOT_SERVICES_APPLICATION", "sd-boot")

	// Step 4: Authenticode hash of the UKI
	ukiHash, err := authentihashFromPath(ukiPath)
	if err != nil {
		return fmt.Errorf("computing UKI authentihash: %w", err)
	}
	sim.Extend(4, ukiHash, "EV_EFI_BOOT_SERVICES_APPLICATION", "UKI")

	// Step 5: Authenticode hash of the Linux kernel
	linuxHash, err := authentihashFromPath(linuxPath)
	if err != nil {
		return fmt.Errorf("computing linux kernel authentihash: %w", err)
	}
	sim.Extend(4, linuxHash, "EV_EFI_BOOT_SERVICES_APPLICATION", "linux kernel")

	return nil
}

func authentihashFromPath(path string) ([32]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return [32]byte{}, fmt.Errorf("opening %s: %w", path, err)
	}
	defer f.Close()

	return Authentihash(f)
}
