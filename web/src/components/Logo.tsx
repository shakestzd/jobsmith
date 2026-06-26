/**
 * Logo — inline J-Rise SVG mark for Jobsmith.
 *
 * Props:
 *   size    — rendered width/height in px (default 28)
 *   variant — 'default'  → Indigo Ink shaft (#2C2E72) + Forge Amber tip (#F4A024)
 *             'reversed' → white shaft (#ffffff) + Forge Amber tip (#F4A024)
 *             'mono'     → Indigo Ink shaft + Indigo Ink tip (single-color)
 */

export type LogoVariant = 'default' | 'reversed' | 'mono';

interface LogoProps {
  size?: number;
  variant?: LogoVariant;
}

const INK_DEFAULT = '#2C2E72';
const INK_REVERSED = '#ffffff';
const AMBER = '#F4A024';

export default function Logo({ size = 28, variant = 'default' }: LogoProps) {
  const shaftColor =
    variant === 'reversed' ? INK_REVERSED : INK_DEFAULT;
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
