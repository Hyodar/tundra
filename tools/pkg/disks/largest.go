package disks

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"unicode"
)

// ProcPartitionsPath is the path to /proc/partitions. Tests can override it.
var ProcPartitionsPath = "/proc/partitions"

// LargestDiskFinder finds the SCSI disk (sd*) with the most capacity by
// parsing /proc/partitions.
type LargestDiskFinder struct{}

var _ DiskFinder = (*LargestDiskFinder)(nil)

// Find returns the device path (e.g. /dev/sdb) of the largest SCSI disk.
func (f *LargestDiskFinder) Find() (string, error) {
	file, err := os.Open(ProcPartitionsPath)
	if err != nil {
		return "", fmt.Errorf("largest disk finder: %w", err)
	}
	defer file.Close()

	return parseLargestDisk(file)
}

// parseLargestDisk reads /proc/partitions content and returns the largest
// whole SCSI disk device path.
func parseLargestDisk(r io.Reader) (string, error) {
	scanner := bufio.NewScanner(r)

	var bestName string
	var bestBlocks int64

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}

		name := fields[3]

		// Only consider SCSI disks (names starting with "sd").
		if !strings.HasPrefix(name, "sd") {
			continue
		}

		// Exclude partitions: partition names end with a digit (e.g. sda1).
		if len(name) > 0 && unicode.IsDigit(rune(name[len(name)-1])) {
			continue
		}

		blocks, err := strconv.ParseInt(fields[2], 10, 64)
		if err != nil {
			continue
		}

		if blocks > bestBlocks {
			bestBlocks = blocks
			bestName = name
		}
	}

	if err := scanner.Err(); err != nil {
		return "", fmt.Errorf("largest disk finder: reading partitions: %w", err)
	}

	if bestName == "" {
		return "", fmt.Errorf("largest disk finder: no SCSI disk found")
	}

	return "/dev/" + bestName, nil
}
