package measure

// PESection describes a section from a PE/COFF file along with its
// measurement metadata for UKI (Unified Kernel Image) PCR 11 prediction.
type PESection struct {
	Name         string
	Size         uint32
	Digest       [32]byte
	Measure      bool // whether this section is measured into PCR 11
	MeasureOrder int  // ordering for measurement (lower = first)
}

// ukiMeasuredSections maps section names to their measurement order.
// These are the UKI PE sections that systemd-stub measures into PCR 11.
// The .pcrsig section is explicitly NOT measured.
var ukiMeasuredSections = map[string]int{
	".linux":   0,
	".osrel":   1,
	".cmdline": 2,
	".initrd":  3,
	".splash":  4,
	".dtb":     5,
	".uname":   6,
	".sbat":    7,
	".pcrkey":  8,
}

// NullTerminatedName returns the section name as a byte slice with a
// trailing NUL byte appended, matching how systemd-stub formats
// section names before hashing them for PCR 11 measurement.
func NullTerminatedName(name string) []byte {
	b := make([]byte, len(name)+1)
	copy(b, name)
	// b[len(name)] is already 0x00 from make()
	return b
}
