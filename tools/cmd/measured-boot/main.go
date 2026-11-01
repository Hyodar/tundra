package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/Hyodar/tundra/tools/pkg/measure"
)

const usage = `Usage: measured-boot <image-or-uki-file> <output-file> [--direct-uki]

Pre-calculates TPM2 PCR values for a UEFI boot system.

Arguments:
  image-or-uki-file  Path to a disk image or a UKI file (with --direct-uki)
  output-file        Path to write the JSON output

Options:
  --direct-uki       Treat the input as a UKI file directly instead of a disk image
`

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	args := os.Args[1:]

	directUKI := false
	positional := []string{}
	for _, arg := range args {
		if arg == "--direct-uki" {
			directUKI = true
		} else if arg == "--help" || arg == "-h" {
			fmt.Print(usage)
			return nil
		} else {
			positional = append(positional, arg)
		}
	}

	if len(positional) < 2 {
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("insufficient positional arguments")
	}

	inputPath := positional[0]
	outputPath := positional[1]

	ctx := context.Background()

	if directUKI {
		return predictFromUKI(ctx, inputPath, outputPath)
	}
	return predictFromImage(ctx, inputPath, outputPath)
}

// predictFromImage extracts EFI binaries from a disk image and predicts
// PCR values for the boot chain.
func predictFromImage(ctx context.Context, imagePath, outputPath string) error {
	tmpDir, err := os.MkdirTemp("", "measured-boot-*")
	if err != nil {
		return fmt.Errorf("creating temp dir: %w", err)
	}
	defer os.RemoveAll(tmpDir)

	// Extract sd-boot from the image.
	sdBootPath := filepath.Join(tmpDir, "bootx64.efi")
	if err := measure.CopyFrom(ctx, imagePath, "/EFI/BOOT/bootx64.efi", sdBootPath); err != nil {
		return fmt.Errorf("extracting sd-boot: %w", err)
	}

	// Extract UKI from the image.
	// The UKI is located at /EFI/Linux/*.efi - we need to find it.
	// For now, try a well-known path pattern.
	ukiPath, err := extractUKI(ctx, imagePath, tmpDir)
	if err != nil {
		return fmt.Errorf("extracting UKI: %w", err)
	}

	return predictWithFiles(ctx, sdBootPath, ukiPath, outputPath)
}

// extractUKI tries to extract the UKI from a disk image.
// It searches /EFI/Linux/ for *.efi files.
func extractUKI(ctx context.Context, imagePath, tmpDir string) (string, error) {
	// Try a common UKI filename pattern. In practice, the UKI filename
	// varies by distribution. We try extracting a listing first via
	// systemd-dissect, then fall back to common names.
	ukiDest := filepath.Join(tmpDir, "uki.efi")

	// Try to list and extract from /EFI/Linux/
	// First attempt: use systemd-dissect --list to find the UKI.
	entries, err := listDir(ctx, imagePath, "/EFI/Linux")
	if err == nil {
		for _, entry := range entries {
			if filepath.Ext(entry) == ".efi" {
				internalPath := "/EFI/Linux/" + entry
				if err := measure.CopyFrom(ctx, imagePath, internalPath, ukiDest); err == nil {
					return ukiDest, nil
				}
			}
		}
	}

	// Fallback: try the path directly.
	if err := measure.CopyFrom(ctx, imagePath, "/EFI/Linux/linux.efi", ukiDest); err != nil {
		return "", fmt.Errorf("could not find UKI in disk image: %w", err)
	}
	return ukiDest, nil
}

// listDir uses systemd-dissect to list directory contents in a disk image.
func listDir(ctx context.Context, imagePath, dir string) ([]string, error) {
	// This is a best-effort function. systemd-dissect --list can list files.
	// For now, we return an error to trigger the fallback.
	return nil, fmt.Errorf("directory listing not implemented")
}

// predictFromUKI treats the input directly as a UKI file.
func predictFromUKI(ctx context.Context, ukiPath, outputPath string) error {
	return predictWithFiles(ctx, "", ukiPath, outputPath)
}

// predictWithFiles computes PCR predictions given the paths to EFI binaries.
// If sdBootPath is empty, PCR 4 measurement skips the sd-boot and uses only
// the UKI and embedded Linux kernel.
func predictWithFiles(ctx context.Context, sdBootPath, ukiPath, outputPath string) error {
	sim := measure.NewSimulator()

	// Parse UKI sections and compute digests.
	sections, err := measure.PeSectionDigests(ukiPath)
	if err != nil {
		return fmt.Errorf("parsing UKI sections: %w", err)
	}

	// Extract the Linux kernel from the UKI .linux section to a temp file
	// for Authenticode hashing.
	linuxPath, cleanup, err := extractLinuxFromUKI(ukiPath)
	if err != nil {
		return fmt.Errorf("extracting linux kernel from UKI: %w", err)
	}
	defer cleanup()

	// PCR 4: Boot chain.
	if sdBootPath != "" {
		if err := measure.PredictPCR4(sim, sdBootPath, ukiPath, linuxPath); err != nil {
			return fmt.Errorf("predicting PCR 4: %w", err)
		}
	} else {
		// Direct UKI mode: only measure UKI and linux kernel.
		if err := predictPCR4DirectUKI(sim, ukiPath, linuxPath); err != nil {
			return fmt.Errorf("predicting PCR 4 (direct UKI): %w", err)
		}
	}

	// PCR 9: Kernel command line and initrd.
	cmdline, err := readUKISection(ukiPath, ".cmdline")
	if err != nil {
		// cmdline may not exist; use empty.
		cmdline = nil
	}
	initrd, err := readUKISection(ukiPath, ".initrd")
	if err != nil {
		// initrd may not exist; use empty.
		initrd = nil
	}
	if cmdline != nil || initrd != nil {
		if cmdline == nil {
			cmdline = []byte{}
		}
		if initrd == nil {
			initrd = []byte{}
		}
		if err := measure.PredictPCR9(sim, cmdline, initrd); err != nil {
			return fmt.Errorf("predicting PCR 9: %w", err)
		}
	}

	// PCR 11: UKI sections.
	if err := measure.PredictPCR11(sim, sections); err != nil {
		return fmt.Errorf("predicting PCR 11: %w", err)
	}

	// PCRs 8, 12, 13, 15 are left at their initial zero values.

	// Write output.
	out, err := json.MarshalIndent(sim, "", "  ")
	if err != nil {
		return fmt.Errorf("marshaling output: %w", err)
	}

	if err := os.WriteFile(outputPath, out, 0644); err != nil {
		return fmt.Errorf("writing output: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Wrote PCR predictions to %s\n", outputPath)
	return nil
}

// predictPCR4DirectUKI measures PCR 4 when given a UKI directly (no sd-boot).
func predictPCR4DirectUKI(sim *measure.Simulator, ukiPath, linuxPath string) error {
	sim.Extend(4, measure.EVEFIAction, "EV_EFI_ACTION", "Calling EFI Application from Boot Option")
	sim.Extend(4, measure.EVSeparator, "EV_SEPARATOR", "separator")

	ukiHash, err := authentihashPath(ukiPath)
	if err != nil {
		return fmt.Errorf("computing UKI authentihash: %w", err)
	}
	sim.Extend(4, ukiHash, "EV_EFI_BOOT_SERVICES_APPLICATION", "UKI")

	linuxHash, err := authentihashPath(linuxPath)
	if err != nil {
		return fmt.Errorf("computing linux kernel authentihash: %w", err)
	}
	sim.Extend(4, linuxHash, "EV_EFI_BOOT_SERVICES_APPLICATION", "linux kernel")

	return nil
}

func authentihashPath(path string) ([32]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return [32]byte{}, err
	}
	defer f.Close()
	return measure.Authentihash(f)
}

// extractLinuxFromUKI extracts the .linux section from a UKI to a temp file.
// Returns the path to the temp file and a cleanup function.
func extractLinuxFromUKI(ukiPath string) (string, func(), error) {
	noop := func() {}

	reader, err := measure.PeSectionReader(ukiPath, ".linux")
	if err != nil {
		return "", noop, fmt.Errorf("reading .linux section: %w", err)
	}

	tmpFile, err := os.CreateTemp("", "measured-boot-linux-*.efi")
	if err != nil {
		return "", noop, fmt.Errorf("creating temp file: %w", err)
	}

	if _, err := tmpFile.ReadFrom(reader); err != nil {
		tmpFile.Close()
		os.Remove(tmpFile.Name())
		return "", noop, fmt.Errorf("writing .linux section: %w", err)
	}

	if err := tmpFile.Close(); err != nil {
		os.Remove(tmpFile.Name())
		return "", noop, fmt.Errorf("closing temp file: %w", err)
	}

	cleanup := func() { os.Remove(tmpFile.Name()) }
	return tmpFile.Name(), cleanup, nil
}

// readUKISection reads a section from a UKI file into a byte slice.
func readUKISection(ukiPath, sectionName string) ([]byte, error) {
	reader, err := measure.PeSectionReader(ukiPath, sectionName)
	if err != nil {
		return nil, err
	}
	var buf []byte
	tmp := make([]byte, 32*1024)
	for {
		n, readErr := reader.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if readErr != nil {
			if readErr != io.EOF {
				return nil, fmt.Errorf("reading section %s: %w", sectionName, readErr)
			}
			break
		}
	}
	return buf, nil
}
