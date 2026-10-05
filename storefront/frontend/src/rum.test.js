import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@datadog/browser-rum', () => ({
  datadogRum: { init: vi.fn(), setGlobalContext: vi.fn(), setGlobalContextProperty: vi.fn(), addAction: vi.fn() },
}));
import { datadogRum } from '@datadog/browser-rum';
import { initRum, isRumEnabled, recordStockAnswer, recordStockError, resetRumForTests } from './rum.js';

const RUM = { application_id: 'app', client_token: 'tok', site: 'datadoghq.eu', service: 'storefront-web',
  env: 'dd-demo-dev', version: '1.0.0', stack: 'dev', exposure_budget_ms: 800 };
const ans = (o = {}) => ({ product_id: 'P0042', status: 'available', sellable: 9, at_least: false, feed: 'ok', release: '1.1.0', ...o });

beforeEach(() => {
  vi.clearAllMocks();
  resetRumForTests();
});

describe('initRum', () => {
  it('does nothing without a rum block', () => {
    expect(initRum(undefined)).toBe(false);
    expect(initRum(null)).toBe(false);
    expect(datadogRum.init).not.toHaveBeenCalled();
    recordStockAnswer(ans({ status: 'unknown' }), 5000);
    expect(datadogRum.addAction).not.toHaveBeenCalled();
    expect(isRumEnabled()).toBe(false);
  });

  it('initialises once with the demo configuration and global context', () => {
    expect(initRum(RUM, 'https://shop.example')).toBe(true);
    expect(initRum(RUM)).toBe(false);
    expect(datadogRum.init).toHaveBeenCalledTimes(1);
    const c = datadogRum.init.mock.calls[0][0];
    expect(c).toMatchObject({ applicationId: 'app', clientToken: 'tok', site: 'datadoghq.eu', service: 'storefront-web',
      env: 'dd-demo-dev', version: '1.0.0', sessionSampleRate: 100, trackUserInteractions: true, trackResources: true,
      trackLongTasks: true, defaultPrivacyLevel: 'mask-user-input' });
    const [opt] = c.allowedTracingUrls;
    expect(opt.propagatorTypes).toEqual(['datadog', 'tracecontext']);
    expect(opt.match('https://shop.example/api/availability/P0042')).toBe(true);
    expect(opt.match('https://other.example/api/x')).toBe(false);
    expect(datadogRum.setGlobalContext).toHaveBeenCalledWith({ project: 'dd-demo', stack: 'dev', layer: 'dd-rum' });
  });

  it('fails loudly on an incomplete rum block', () => {
    expect(() => initRum({ ...RUM, client_token: '' })).toThrow(/client_token/);
  });
});

describe('exposure', () => {
  beforeEach(() => initRum(RUM));

  it('a healthy fast answer is not exposed', () => {
    recordStockAnswer(ans(), 120);
    expect(datadogRum.addAction).toHaveBeenCalledWith('stock_answer', expect.objectContaining({ status: 'available', exposed: false, lookup_ms: 120 }));
    expect(datadogRum.setGlobalContextProperty).not.toHaveBeenCalled();
  });

  it.each([
    ['unknown status', ans({ status: 'unknown' }), 100],
    ['at_least', ans({ at_least: true }), 100],
    ['feed not ok', ans({ feed: 'stale' }), 100],
    ['slow lookup', ans(), 801],
  ])('%s marks the session exposed once', (_n, a, ms) => {
    recordStockAnswer(a, ms);
    recordStockAnswer(a, ms);
    expect(datadogRum.addAction).toHaveBeenCalledTimes(1);
    expect(datadogRum.addAction.mock.calls[0][1].exposed).toBe(true);
    expect(datadogRum.setGlobalContextProperty).toHaveBeenCalledTimes(1);
    expect(datadogRum.setGlobalContextProperty).toHaveBeenCalledWith('exposed', true);
  });

  it('sends a new action when the signature changes and carries no personal fields', () => {
    recordStockAnswer(ans(), 100);
    recordStockAnswer(ans({ status: 'out_of_stock' }), 100);
    expect(datadogRum.addAction).toHaveBeenCalledTimes(2);
    expect(Object.keys(datadogRum.addAction.mock.calls[0][1]).sort()).toEqual(
      ['at_least', 'exposed', 'feed', 'lookup_ms', 'product_id', 'release', 'status']);
  });

  it('a failed lookup is an exposure', () => {
    recordStockError('P0042', 40);
    expect(datadogRum.addAction).toHaveBeenCalledWith('stock_answer', expect.objectContaining({ status: 'error', exposed: true }));
  });
});
