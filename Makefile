CXX ?= c++
CPPFLAGS ?=
CXXFLAGS ?= -O2 -g
WARN = -std=c++20 -Wall -Wextra -Wpedantic -Werror
LDLIBS = -lm -pthread

.PHONY: all test check clean
all: build/sched-work build/thread-pipeline

build/sched-work: native/work.cpp native/media.hpp
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(WARN) $< -o $@ $(LDFLAGS) $(LDLIBS)

build/thread-pipeline: native/pipeline.cpp native/media.hpp
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(WARN) $< -o $@ $(LDFLAGS) $(LDLIBS)

test check: all
	python3 -m unittest discover -s tests -v

clean:
	rm -f build/sched-work build/thread-pipeline
