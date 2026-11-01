package measure

import (
	"bytes"
	"context"
	"crypto/sha256"
	"debug/pe"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sort"
)

// CopyFrom extracts a file from a disk image at internalPath and writes it
// to destPath. It tries systemd-dissect first, and falls back to using
// a combination of losetup/mount if that is not available.
func CopyFrom(ctx context.Context, imagePath, internalPath, destPath string) error {
	// Try systemd-dissect first.
	if err := copyWithDissect(ctx, imagePath, internalPath, destPath); err == nil {
		return nil
	}

	// Fallback: try mcopy (from mtools) for FAT partitions, or a simple
	// mount approach. For now we return an error indicating that
	// systemd-dissect is required.
	return fmt.Errorf("systemd-dissect failed or not available; install systemd-dissect to extract files from disk images")
}

func copyWithDissect(ctx context.Context, imagePath, internalPath, destPath string) error {
	cmd := exec.CommandContext(ctx, "systemd-dissect",
		"--copy-from", imagePath, internalPath, destPath)
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// PeSectionDigests opens a PE file and computes the SHA-256 digest of every
// section's raw data. It returns the sections sorted with measured sections
// first (ordered by MeasureOrder), then unmeasured sections in alphabetical
// order.
func PeSectionDigests(filePath string) ([]PESection, error) {
	f, err := pe.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("opening PE file %s: %w", filePath, err)
	}
	defer f.Close()

	var sections []PESection
	for _, sec := range f.Sections {
		data, err := sec.Data()
		if err != nil {
			return nil, fmt.Errorf("reading section %s: %w", sec.Name, err)
		}

		ps := PESection{
			Name:   sec.Name,
			Size:   sec.Size,
			Digest: sha256.Sum256(data),
		}

		if order, ok := ukiMeasuredSections[sec.Name]; ok {
			ps.Measure = true
			ps.MeasureOrder = order
		}

		sections = append(sections, ps)
	}

	sort.SliceStable(sections, func(i, j int) bool {
		mi := sections[i].Measure
		mj := sections[j].Measure
		if mi && mj {
			return sections[i].MeasureOrder < sections[j].MeasureOrder
		}
		if mi != mj {
			return mi
		}
		// Both unmeasured: sort alphabetically.
		return sections[i].Name < sections[j].Name
	})

	return sections, nil
}

// PeSectionReader opens a PE file and returns an io.Reader for the named
// section's raw data.
func PeSectionReader(filePath, sectionName string) (io.Reader, error) {
	f, err := pe.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("opening PE file %s: %w", filePath, err)
	}
	defer f.Close()

	sec := f.Section(sectionName)
	if sec == nil {
		return nil, fmt.Errorf("section %s not found in %s", sectionName, filePath)
	}

	data, err := sec.Data()
	if err != nil {
		return nil, fmt.Errorf("reading section %s: %w", sectionName, err)
	}

	return bytes.NewReader(data), nil
}
