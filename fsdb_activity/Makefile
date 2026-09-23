# Build fsdb_activity against the Synopsys NPI reader.
#
#   Verdi install (the usual case):   make VERDI_HOME=/path/to/verdi
#   FSDB+ development kit:            make FSDB_PLUS_HOME=/path/to/Y-2026.03
#
# Running needs the matching license (Verdi, or FSDB+DevKit) in
# LM_LICENSE_FILE / SNPSLMD_LICENSE_FILE.

CXX      ?= g++
CXXFLAGS ?= -O2
CXXFLAGS += -std=c++11 -Wall

ifdef FSDB_PLUS_HOME
  NPI_INC := $(FSDB_PLUS_HOME)/include
  NPI_LIB := $(FSDB_PLUS_HOME)/lib/linux64
  DEFS    := -DNPI_FPLUS
  LIBS    := -lNPI -lnpiL1 -lvfs
else ifdef VERDI_HOME
  NPI_INC := $(VERDI_HOME)/share/NPI/inc
  NPI_LIB := $(VERDI_HOME)/share/NPI/lib/LINUX64
  DEFS    :=
  LIBS    := -lNPI
else
  $(error set VERDI_HOME (Verdi install) or FSDB_PLUS_HOME (FSDB+ SDK))
endif

fsdb_activity: fsdb_activity.cpp
	$(CXX) $(CXXFLAGS) $(DEFS) -DNPI_LIBDIR='"$(NPI_LIB)"' -I$(NPI_INC) $< -o $@ \
	    -L$(NPI_LIB) -Wl,-rpath,$(NPI_LIB) $(LIBS)

clean:
	rm -f fsdb_activity

.PHONY: clean
