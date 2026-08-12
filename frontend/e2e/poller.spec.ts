import { expect, test } from '@playwright/test';

import { Poller } from '../src/lib/latest';

// Poller's contract has one property that keeps breaking: every way a run
// can END must also SETTLE it. Three review passes found three different
// members of that family - an orphaned timer firing after teardown, a
// second run overwriting the first's handle, and finally a cancelled timer
// resolving nothing so run() awaited forever. The last one silently lost a
// dropped Strava export, because both real callers await run() to do their
// after-work.
//
// So this file is not about polling. It is about that one property, checked
// on every exit path there is. No browser is needed - it is a plain class -
// and Playwright's runner is used only because this repo deliberately has no
// unit-test framework and is not gaining one.

/** Resolves true if `promise` settles within `ms`, false if it hangs. */
async function settles(promise: Promise<unknown>, ms = 1000): Promise<boolean> {
	return Promise.race([
		promise.then(() => true),
		new Promise<boolean>((resolve) => setTimeout(() => resolve(false), ms))
	]);
}

test.describe('Poller always settles the run it ends', () => {
	test('when the step asks it to stop', async () => {
		const poller = new Poller();
		expect(await settles(poller.run(async () => false, 10))).toBe(true);
	});

	test('when stopped while the timer is armed', async () => {
		// The common case by far: a poller spends nearly all of every
		// interval waiting rather than stepping, so this is the state
		// onDestroy almost always finds it in.
		const poller = new Poller();
		const running = poller.run(async () => true, 10_000);
		setTimeout(() => poller.stop(), 20);
		expect(await settles(running)).toBe(true);
	});

	test('when superseded by a second run on the same instance', async () => {
		// The loser must still resolve. Both real callers await run() and
		// then do their after-work; a run that never resolves means the
		// after-work never happens, which is how a second dropped archive
		// was silently never imported.
		const poller = new Poller();
		const first = poller.run(async () => true, 10_000);
		await new Promise((resolve) => setTimeout(resolve, 20));
		void poller.run(async () => false, 10);
		expect(await settles(first)).toBe(true);
	});

	test('when the step throws synchronously', async () => {
		const poller = new Poller();
		const thrower = (): Promise<boolean> => {
			throw new Error('a non-async step can do this');
		};
		expect(await settles(poller.run(thrower, 10))).toBe(true);
	});

	test('when the step rejects', async () => {
		const poller = new Poller();
		const rejecter = async (): Promise<boolean> => {
			throw new Error('a failed request');
		};
		expect(await settles(poller.run(rejecter, 10))).toBe(true);
	});

	test('when stopped before it ever ran', async () => {
		const poller = new Poller();
		poller.stop();
		expect(await settles(poller.run(async () => true, 10))).toBe(true);
	});

	test('and stops stepping once stopped', async () => {
		const poller = new Poller();
		let steps = 0;
		const running = poller.run(async () => {
			steps += 1;
			return true;
		}, 10);
		setTimeout(() => poller.stop(), 55);
		await running;

		const afterStop = steps;
		await new Promise((resolve) => setTimeout(resolve, 80));
		expect(steps).toBe(afterStop);
	});
});

test.describe('Latest hands ownership to the newest claim', () => {
	test('an older claim is no longer current', async () => {
		const { Latest } = await import('../src/lib/latest');
		const owner = new Latest();
		const first = owner.claim();
		const second = owner.claim();
		expect(owner.isCurrent(first)).toBe(false);
		expect(owner.isCurrent(second)).toBe(true);
	});
});
