// A GC lock timeout is explicitly returned as 503. Keep retries local to one
// feed request: two delays, three attempts, then leave recovery to the reader.
// Integrity/expiry errors and uncertain transport failures are not retried.
export async function readFeedWithRetry<T>(
  read: () => Promise<T>,
  current: () => boolean,
): Promise<T | undefined> {
  const delays = [250, 750];
  for (let attempt = 0; current(); attempt += 1) {
    try {
      const value = await read();
      return current() ? value : undefined;
    } catch (error) {
      if (!current()) return undefined;
      if (
        (error as { status?: number })?.status !== 503 ||
        attempt >= delays.length
      )
        throw error;
      await new Promise<void>((resolve) =>
        setTimeout(resolve, delays[attempt]),
      );
    }
  }
  return undefined;
}
