/** A server-side refusal (or a local failure) with the same codes the Python server uses. */
export class CoordError extends Error {
    code;
    data;
    constructor(code, message, data = null) {
        super(message);
        this.code = code;
        this.data = data;
        this.name = "CoordError";
    }
}
