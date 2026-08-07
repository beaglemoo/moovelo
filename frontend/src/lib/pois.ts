// What the indexer writes into pois.category, grouped into something worth
// putting on screen as filter chips.
//
// Measured against a real Tring to Oxford ride: 722 POIs within 250 m, of
// which 232 are unnamed bike parking, 167 takeaways and 123 cafes - while
// drinking water, a repair stand and a picnic site number one each. Show all
// of that at once and the rare things you actually go looking for are
// unfindable, so the default is the four that matter on a ride and the rest
// are a click away.

export interface PoiGroup {
	key: string;
	label: string;
	categories: string[];
	colour: string;
}

export const POI_GROUPS: PoiGroup[] = [
	{ key: 'water', label: 'Water', categories: ['water'], colour: '#268bd2' },
	{ key: 'coffee', label: 'Coffee', categories: ['cafe', 'bakery'], colour: '#b58900' },
	{ key: 'toilets', label: 'Toilets', categories: ['toilets'], colour: '#6c71c4' },
	{ key: 'bike', label: 'Bike', categories: ['bike_shop', 'bike_repair'], colour: '#d33682' },
	{
		key: 'food',
		label: 'Food',
		categories: ['food', 'supermarket', 'shop', 'pharmacy'],
		colour: '#cb4b16'
	},
	{ key: 'pub', label: 'Pub', categories: ['pub'], colour: '#859900' },
	{ key: 'rest', label: 'Rest', categories: ['viewpoint', 'picnic'], colour: '#2aa198' },
	{ key: 'stay', label: 'Stay', categories: ['campsite', 'accommodation'], colour: '#586e75' }
];

// The four you plan a ride around. The others are opt-in per ride.
export const DEFAULT_POI_GROUPS = ['water', 'coffee', 'toilets', 'bike'];

// bike_parking is indexed but deliberately absent above: 57,950 rows of
// which 1,021 have a name, so it would swamp every list it appeared in.
// Add it to the Bike group if you ever want it - nothing else changes.

const LABELS: Record<string, string> = {
	water: 'Drinking water',
	cafe: 'Cafe',
	bakery: 'Bakery',
	toilets: 'Toilets',
	bike_shop: 'Bike shop',
	bike_repair: 'Repair stand',
	food: 'Food',
	supermarket: 'Supermarket',
	shop: 'Shop',
	pharmacy: 'Pharmacy',
	pub: 'Pub',
	viewpoint: 'Viewpoint',
	picnic: 'Picnic site',
	campsite: 'Campsite',
	accommodation: 'Accommodation',
	bike_parking: 'Bike parking'
};

export function categoryLabel(category: string): string {
	return LABELS[category] ?? category.replace(/_/g, ' ');
}

const COLOUR_BY_CATEGORY: Record<string, string> = Object.fromEntries(
	POI_GROUPS.flatMap((group) => group.categories.map((c) => [c, group.colour]))
);

export function categoryColour(category: string): string {
	return COLOUR_BY_CATEGORY[category] ?? '#93a1a1';
}

/** The flat category names the API takes, for a set of selected groups. */
export function categoriesFor(groupKeys: string[]): string[] {
	return POI_GROUPS.filter((g) => groupKeys.includes(g.key)).flatMap((g) => g.categories);
}

/** A MapLibre `match` expression colouring circles by category. */
export function colourExpression(): unknown[] {
	const match: unknown[] = ['match', ['get', 'category']];
	for (const [category, colour] of Object.entries(COLOUR_BY_CATEGORY)) {
		match.push(category, colour);
	}
	match.push('#93a1a1');
	return match;
}
