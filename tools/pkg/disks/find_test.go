package disks

import (
	"strings"
	"testing"
)

func TestParseLargestDisk(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    string
		wantErr bool
	}{
		{
			name: "single scsi disk",
			input: `major minor  #blocks  name
   8        0  104857600 sda
`,
			want: "/dev/sda",
		},
		{
			name: "multiple scsi disks picks largest",
			input: `major minor  #blocks  name
   8        0  104857600 sda
   8       16  209715200 sdb
   8       32   52428800 sdc
`,
			want: "/dev/sdb",
		},
		{
			name: "excludes partitions",
			input: `major minor  #blocks  name
   8        0  104857600 sda
   8        1   52428800 sda1
   8        2   52428800 sda2
   8       16  209715200 sdb
`,
			want: "/dev/sdb",
		},
		{
			name: "ignores non-scsi devices",
			input: `major minor  #blocks  name
 253        0  104857600 vda
   8        0   52428800 sda
`,
			want: "/dev/sda",
		},
		{
			name: "no scsi disks found",
			input: `major minor  #blocks  name
 253        0  104857600 vda
 253        1   52428800 vda1
`,
			wantErr: true,
		},
		{
			name:    "empty input",
			input:   "",
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseLargestDisk(strings.NewReader(tt.input))
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tt.want {
				t.Errorf("got %q, want %q", got, tt.want)
			}
		})
	}
}

func TestCreateDiskFinder(t *testing.T) {
	t.Run("largest strategy", func(t *testing.T) {
		cfg := DiskConfig{Strategy: "largest"}
		finder, err := CreateDiskFinder(cfg)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if _, ok := finder.(*LargestDiskFinder); !ok {
			t.Errorf("expected LargestDiskFinder, got %T", finder)
		}
	})

	t.Run("pathglob strategy", func(t *testing.T) {
		cfg := DiskConfig{
			Strategy:       "pathglob",
			StrategyConfig: map[string]string{"pattern": "/dev/sd*"},
		}
		finder, err := CreateDiskFinder(cfg)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if f, ok := finder.(*PathGlobFinder); !ok {
			t.Errorf("expected PathGlobFinder, got %T", finder)
		} else if f.Pattern != "/dev/sd*" {
			t.Errorf("pattern = %q, want %q", f.Pattern, "/dev/sd*")
		}
	})

	t.Run("pathglob missing pattern", func(t *testing.T) {
		cfg := DiskConfig{
			Strategy: "pathglob",
		}
		_, err := CreateDiskFinder(cfg)
		if err == nil {
			t.Fatal("expected error for missing pattern")
		}
	})

	t.Run("unsupported strategy", func(t *testing.T) {
		cfg := DiskConfig{Strategy: "random"}
		_, err := CreateDiskFinder(cfg)
		if err == nil {
			t.Fatal("expected error for unsupported strategy")
		}
	})
}
