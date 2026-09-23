/** A server-side refusal (or a local failure) with the same codes the Python server uses. */
export class CoordError extends Error {
  constructor(public readonly code: string, message: string, public readonly data: unknown = null) {
    super(message);
    this.name = "CoordError";
  }
}
