// Logo.test.tsx — unit tests for Logo component
//
// Assertions:
//   tile variant renders indigo background (#2C2E72) with centered glyph
//   tile variant has white shaft (#ffffff) and amber tip (#F4A024)

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import Logo from './Logo';

describe('Logo', () => {
  it('renders default variant as SVG', () => {
    const { container } = render(<Logo size={28} variant="default" />);
    const svg = container.querySelector('svg');
    expect(svg).toBeTruthy();
    expect(svg?.getAttribute('role')).toBe('img');
  });

  it('renders tile variant with indigo squircle background', () => {
    const { container } = render(<Logo size={26} variant="tile" />);
    const wrapper = container.querySelector('div');
    expect(wrapper).toBeTruthy();
    expect(wrapper?.style.backgroundColor).toBe('rgb(44, 46, 114)'); // #2C2E72 converted to rgb
  });

  it('tile variant has correct border-radius (22% of size)', () => {
    const size = 26;
    const expectedRadius = Math.round(size * 0.22);
    const { container } = render(<Logo size={size} variant="tile" />);
    const wrapper = container.querySelector('div');
    expect(wrapper?.style.borderRadius).toBe(`${expectedRadius}px`);
  });

  it('tile variant renders white shaft and amber tip in glyph', () => {
    const { container } = render(<Logo size={26} variant="tile" />);
    const svg = container.querySelector('svg');
    const paths = svg?.querySelectorAll('path');

    expect(paths).toBeTruthy();
    expect(paths?.length).toBe(2);

    // First path is shaft (white)
    const shaft = paths?.[0];
    expect(shaft?.getAttribute('stroke')).toBe('#ffffff');

    // Second path is arrowhead tip (amber)
    const tip = paths?.[1];
    expect(tip?.getAttribute('stroke')).toBe('#F4A024');
  });

  it('tile variant glyph is 60% of tile size', () => {
    const size = 26;
    const expectedGlyphSize = Math.round(size * 0.6);
    const { container } = render(<Logo size={size} variant="tile" />);
    const svg = container.querySelector('svg');
    expect(svg?.getAttribute('width')).toBe(`${expectedGlyphSize}`);
    expect(svg?.getAttribute('height')).toBe(`${expectedGlyphSize}`);
  });

  it('tile variant is centered in wrapper', () => {
    const { container } = render(<Logo size={26} variant="tile" />);
    const wrapper = container.querySelector('div');
    expect(wrapper?.style.display).toBe('flex');
    expect(wrapper?.style.alignItems).toBe('center');
    expect(wrapper?.style.justifyContent).toBe('center');
  });
});
