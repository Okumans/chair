#pragma once

#include <Arduino.h>

#include <optional>
#include <string>

#include "glaze/json/read.hpp"
#include "glaze/json/write.hpp"

namespace Transport {
  struct UsbCommand {
    std::string kind;
    std::optional<uint32_t> request_id;
    std::optional<std::string> data_hex;
  };

  struct UsbReadOptions : glz::opts {
    bool error_on_unknown_keys        = false;
    bool validate_skipped             = true;
    bool validate_trailing_whitespace = true;
  };

  inline void printJson(const auto& value) {
    std::string buffer;
    if (glz::write_json(value, buffer)) {
      Serial.println("{\"kind\":\"error\",\"request_id\":0,\"reason\":\"json_encode_failed\"}");
      return;
    }
    Serial.print(buffer.c_str());
    Serial.print('\n');
  }
}  // namespace Transport
