#pragma once
#include <concepts>
#include <cstddef>
#include <optional>
#include <ostream>
#include <ranges>
#include <string_view>
#include <type_traits>

// Minimal streaming JSON output: typed values instead of printf formats, escaping in one place.
namespace lab::json {

// Quotes, backslashes and control characters are escaped; other bytes are written unchanged.
inline void write_escaped(std::ostream &out, std::string_view text) {
    constexpr std::string_view hex = "0123456789abcdef";
    for (const char character : text) {
        const auto byte = static_cast<unsigned char>(character);
        if (character == '"' || character == '\\') {
            out << '\\' << character;
        } else if (byte < 0x20U) {
            out << "\\u00" << hex[byte >> 4U] << hex[byte & 0xFU];
        } else {
            out << character;
        }
    }
}

inline void write_value(std::ostream &out, std::string_view text) {
    out << '"';
    write_escaped(out, text);
    out << '"';
}
inline void write_value(std::ostream &out, const char *text) { write_value(out, std::string_view(text)); }
inline void write_value(std::ostream &out, bool value) { out << (value ? "true" : "false"); }
inline void write_value(std::ostream &out, std::nullptr_t) { out << "null"; }

template<std::integral T>
    requires(!std::same_as<T, bool>)
void write_value(std::ostream &out, T value) {
    // Widen first so that char-sized integers print as numbers, not characters.
    if constexpr (std::is_signed_v<T>)
        out << static_cast<long long>(value);
    else
        out << static_cast<unsigned long long>(value);
}

template<typename T>
void write_value(std::ostream &out, const std::optional<T> &value) {
    if (value)
        write_value(out, *value);
    else
        out << "null";
}

template<std::ranges::input_range Range>
    requires(!std::convertible_to<const Range &, std::string_view>)
void write_value(std::ostream &out, const Range &values) {
    out << '[';
    bool first = true;
    for (const auto &value : values) {
        if (!first) out << ',';
        first = false;
        write_value(out, value);
    }
    out << ']';
}

// One object; `{` is written on construction and `}` on destruction, so nested objects close
// at the end of their scope or full expression.
class Object {
public:
    explicit Object(std::ostream &out) : out_(out) { out_ << '{'; }
    Object(const Object &) = delete;
    Object &operator=(const Object &) = delete;
    Object(Object &&) = delete;
    Object &operator=(Object &&) = delete;
    ~Object() { out_ << '}'; }

    template<typename T>
    Object &field(std::string_view name, const T &value) {
        key(name);
        write_value(out_, value);
        return *this;
    }
    [[nodiscard]] Object object(std::string_view name) {
        key(name);
        return Object(out_);
    }

private:
    void key(std::string_view name) {
        if (!first_) out_ << ',';
        first_ = false;
        write_value(out_, name);
        out_ << ':';
    }

    std::ostream &out_;
    bool first_ = true;
};

} // namespace lab::json
