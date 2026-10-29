#pragma once
// Table-driven "--name value" / "--name=value" parsing shared by the native programs.
#include <array>
#include <charconv>
#include <cstddef>
#include <iostream>
#include <optional>
#include <span>
#include <string_view>
#include <system_error>
#include <vector>

#include <sched.h>

namespace lab::options {

enum class Status { run, help, build_info, invalid };

// Each handler validates and stores one option, explaining a rejected value on stderr.
template<typename State>
struct Spec {
    std::string_view name;
    bool takes_value;
    bool (*apply)(State &state, std::string_view name, std::string_view value);
};

inline std::optional<int> number(std::string_view input, long min, long max, std::string_view name) {
    long n = 0;
    const auto [end, error] = std::from_chars(input.data(), input.data() + input.size(), n);
    if (error != std::errc{} || end != input.data() + input.size() || n < min || n > max) {
        std::cerr << name << " must be in [" << min << ", " << max << "]\n";
        return std::nullopt;
    }
    return static_cast<int>(n);
}

// "0,2-5" -> {0, 2, 3, 4, 5}; every CPU must fit a cpu_set_t.
inline std::optional<std::vector<int>> cpu_list(std::string_view text) {
    std::vector<int> cpus;
    while (!text.empty()) {
        const auto comma = text.find(',');
        const std::string_view part = text.substr(0, comma);
        text = comma == std::string_view::npos ? std::string_view{} : text.substr(comma + 1);
        const auto dash = part.find('-');
        const auto low = number(part.substr(0, dash), 0, CPU_SETSIZE - 1, "cpu");
        const auto high =
            dash == std::string_view::npos ? low : number(part.substr(dash + 1), 0, CPU_SETSIZE - 1, "cpu");
        if (!low || !high || *high < *low) return std::nullopt;
        for (int cpu = *low; cpu <= *high; cpu++) cpus.push_back(cpu);
    }
    if (cpus.empty()) return std::nullopt;
    return cpus;
}

template<typename State, std::size_t N>
const Spec<State> *find(const std::array<Spec<State>, N> &table, std::string_view name) {
    for (const auto &spec : table)
        if (spec.name == name) return &spec;
    return nullptr;
}

// Applies every option in args[1..]; --help, -h and --build-info end parsing immediately.
template<typename State, std::size_t N>
Status parse(std::span<char *const> args, const std::array<Spec<State>, N> &table, State &state) {
    for (std::size_t index = 1; index < args.size(); index++) {
        const std::string_view argument = args[index];
        if (argument == "--help" || argument == "-h") return Status::help;
        if (argument == "--build-info") return Status::build_info;
        if (!argument.starts_with("--")) {
            std::cerr << "unexpected argument: " << argument << '\n';
            return Status::invalid;
        }
        std::string_view name = argument.substr(2);
        std::optional<std::string_view> attached;
        if (const auto equals = name.find('='); equals != std::string_view::npos) {
            attached = name.substr(equals + 1);
            name = name.substr(0, equals);
        }
        const Spec<State> *const spec = find(table, name);
        if (!spec) {
            std::cerr << "unrecognized option '--" << name << "'\n";
            return Status::invalid;
        }
        std::string_view value;
        if (spec->takes_value && attached) {
            value = *attached;
        } else if (spec->takes_value && index + 1 < args.size()) {
            value = args[++index];
        } else if (spec->takes_value || attached) {
            std::cerr << "option '--" << name << (attached ? "' takes no value\n" : "' requires a value\n");
            return Status::invalid;
        }
        if (!spec->apply(state, name, value)) return Status::invalid;
    }
    return Status::run;
}

// Handlers for the common option shapes; Member is a pointer to a field of State's options.
template<typename State, auto Member, long Min, long Max>
bool integer(State &state, std::string_view name, std::string_view value) {
    const auto parsed = number(value, Min, Max, name);
    if (parsed) state.options.*Member = *parsed;
    return parsed.has_value();
}
template<typename State, auto Member, auto Parse>
bool choice(State &state, std::string_view name, std::string_view value) {
    const auto parsed = Parse(value);
    if (!parsed) {
        std::cerr << "invalid " << name << ": " << value << '\n';
        return false;
    }
    state.options.*Member = *parsed;
    return true;
}
template<typename State, auto Member>
bool flag(State &state, std::string_view /*name*/, std::string_view /*value*/) {
    state.options.*Member = true;
    return true;
}

} // namespace lab::options
