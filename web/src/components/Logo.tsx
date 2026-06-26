/**
 * Logo — inline J-Rise SVG mark for Jobsmith.
 *
 * Props:
 *   size    — rendered width/height in px (default 28)
 *   variant — 'default'  → Indigo Ink shaft (#2C2E72) + Forge Amber tip (#F4A024)
 *             'reversed' → white shaft (#ffffff) + Forge Amber tip (#F4A024)
 *             'mono'     → Indigo Ink shaft + Indigo Ink tip (single-color)
 *             'adaptive' → currentColor shaft + Forge Amber tip (#F4A024) (inherits text color)
 *             'tile'     → J-Rise glyph centered in rounded indigo squircle (app icon lockup)
 */

export type LogoVariant = 'default' | 'reversed' | 'mono' | 'adaptive' | 'tile';

interface LogoProps {
  size?: number;
  variant?: LogoVariant;
}

const INK_DEFAULT = '#2C2E72';
const INK_REVERSED = '#ffffff';
const AMBER = '#F4A024';

export default function Logo({ size = 28, variant = 'default' }: LogoProps) {
  if (variant === 'tile') {
    const borderRadius = Math.round(size * 0.22);
    return (
      <div
        style={{
          width: size,
          height: size,
          backgroundColor: '#2C2E72',
          borderRadius,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
        role="img"
        aria-label="Jobsmith"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 100 100"
          width={Math.round(size * 0.6)}
          height={Math.round(size * 0.6)}
          style={{ flexShrink: 0 }}
        >
          {/* shaft + hook */}
          <path
            d="M59,27 L59,60 Q59,79 41,79 Q24,79 24,63"
            fill="none"
            stroke="#ffffff"
            strokeWidth={13}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {/* amber arrowhead tip */}
          <path
            d="M46,32 L59,18 L72,32"
            fill="none"
            stroke="#F4A024"
            strokeWidth={12.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    );
  }

  const shaftColor =
    variant === 'reversed' ? INK_REVERSED :
    variant === 'adaptive' ? 'currentColor' :
    INK_DEFAULT;
  const tipColor =
    variant === 'mono' ? INK_DEFAULT : AMBER;

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 100 100"
      width={size}
      height={size}
      role="img"
      aria-label="Jobsmith"
    >
      {/* shaft + hook */}
      <path
        d="M59,27 L59,60 Q59,79 41,79 Q24,79 24,63"
        fill="none"
        stroke={shaftColor}
        strokeWidth={13}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* amber arrowhead tip */}
      <path
        d="M46,32 L59,18 L72,32"
        fill="none"
        stroke={tipColor}
        strokeWidth={12.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
