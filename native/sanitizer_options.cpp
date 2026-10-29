// Linked into sanitizer builds only (SCHEDLAB_SANITIZE). Any report ends the process with status 99,
// which no lab path uses, so the runner and tests cannot mistake a finding for an expected failure.
// The environment variables (ASAN_OPTIONS, UBSAN_OPTIONS, TSAN_OPTIONS) still override these.
// NOLINTBEGIN(bugprone-reserved-identifier,misc-use-internal-linkage): the runtimes look these names up.
extern "C" const char *__asan_default_options() { return "exitcode=99:detect_stack_use_after_return=1"; }
extern "C" const char *__ubsan_default_options() { return "exitcode=99:halt_on_error=1:print_stacktrace=1"; }
extern "C" const char *__tsan_default_options() {
    return "exitcode=99:halt_on_error=1:second_deadlock_stack=1";
}
// NOLINTEND(bugprone-reserved-identifier,misc-use-internal-linkage)
