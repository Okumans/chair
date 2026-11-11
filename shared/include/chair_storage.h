#pragma once

#include "protocol.h"

namespace SmartChair {
  /// Bounded durable UUID deduplication record, including original content and outcome.
  struct CommandRecord {
    Uuid uuid{};
    Configuration config;
    Status status = Status::applied;
  };

  /// Atomic storage snapshot: configuration, up to 32 reliable events and 32 command records.
  struct Durable {
    Configuration config;
    uint64_t boot_generation = 0;
    std::vector<Frame> outbox;
    std::vector<CommandRecord> commands;
  };

  /// Serialize a trusted engine snapshot into the versioned, explicit byte storage format.
  std::vector<uint8_t> encodeDurable(const Durable& d);

  /// Validate a stored snapshot completely; leave out unchanged on corrupt or unsupported input.
  bool decodeDurable(std::span<const uint8_t> bytes, Durable& out);

}  // namespace SmartChair
