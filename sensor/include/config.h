#pragma once

namespace Config::Storage {
  /// Keep this NVS namespace/key stable across firmware updates; the blob carries its own version.
  inline constexpr char NAMESPACE[] = "chair-v1";
  inline constexpr char STATE_KEY[] = "state";
}  // namespace Config::Storage
