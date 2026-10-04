/** Distinguish a native earnings result from an imported, versioned event set. */
export function eventExportIdentity(
  metadata: Record<string, any>,
  securitySymbol?: string,
  nativeEarnings = false,
): { symbol: string; factVersion: string } {
  const params = metadata.params ?? {};
  const symbol = securitySymbol || metadata.symbol || params.company?.ticker;
  return {
    symbol: symbol ? String(symbol) : "待核对",
    factVersion: metadata.event_version_id
      ? `事件资料版本 ${metadata.event_version_id}`
      : nativeEarnings
        ? "原生财报事实随结果冻结"
        : "事件资料版本未记录（结果仍冻结）",
  };
}
