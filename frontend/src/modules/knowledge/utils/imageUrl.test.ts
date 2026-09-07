import { describe, expect, it } from 'vitest';

import { resolveMarkdownImageSourceFromMap } from './imageUrl';

const mediaUrls = {
  'docs/assets/diagram.png': '/static-files/session/diagram.png?expires=1&sig=test',
  'asset://asset-generated': '/static-files/session/generated.png?expires=1&sig=test',
};

describe('resolveMarkdownImageSourceFromMap', () => {
  it('maps an original Markdown reference to its authorized display URL', () => {
    expect(resolveMarkdownImageSourceFromMap(
      'docs/assets/diagram.png',
      mediaUrls,
    )).toBe('/static-files/session/diagram.png?expires=1&sig=test');
  });

  it('maps an asset reference by media asset id', () => {
    expect(resolveMarkdownImageSourceFromMap(
      'asset://asset-generated',
      mediaUrls,
    )).toBe('/static-files/session/generated.png?expires=1&sig=test');
  });

  it('does not guess by filename when the source reference is different', () => {
    expect(resolveMarkdownImageSourceFromMap(
      'other/diagram.png',
      mediaUrls,
    )).toBe('other/diagram.png');
  });
});
