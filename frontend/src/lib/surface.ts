// The one definition of which Valhalla surface values count as paved -
// shared by the surface bar and the loop candidate cards so they can never
// disagree about the paved percentage of the same ride. services/loop.py
// mirrors this list server-side for candidate scoring.
export const PAVED_SURFACES = ['paved_smooth', 'paved', 'paved_rough'];
