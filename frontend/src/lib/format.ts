/** Shared display formatting, so distances read the same everywhere. */
export function km(metres: number): string {
	return `${(metres / 1000).toFixed(1)} km`;
}
