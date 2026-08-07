/** Category colours for detected climbs, reusing gradient.ts's own band
 * palette so "steep" reads the same whether it is a stretch of the chart or
 * a climb badge: HC and category 1 share the two hottest gradient colours,
 * category 4 the mildest. */

const CATEGORY_COLOURS: Record<string, string> = {
	HC: '#6c1a1a',
	'1': '#dc322f',
	'2': '#cb4b16',
	'3': '#b58900',
	'4': '#859900'
};

export function categoryColour(category: string): string {
	return CATEGORY_COLOURS[category] ?? '#93a1a1';
}

export function categoryLabel(category: string): string {
	return category === 'HC' ? 'HC' : `Cat ${category}`;
}
