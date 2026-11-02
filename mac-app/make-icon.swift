// Draws Radar's 1024x1024 app icon: a dark squircle holding a dot-matrix
// radar with a blue sweep and a few status blips. Usage: swift make-icon.swift out.png

import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

func rgb(_ hex: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: a)
}

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// Squircle body on Apple's icon grid (824pt artwork inside 1024).
let body = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 34, color: rgb(0x000000, 0.45).cgColor)
rgb(0x0d0d10).setFill()
shape.fill()
ctx.restoreGState()
shape.addClip()
NSGradient(colors: [rgb(0x26262d), rgb(0x0b0b0e)])!.draw(in: body, angle: -90)

let c = CGPoint(x: size / 2, y: size / 2)
let R: CGFloat = 318

// Faint range rings.
for r in [R * 0.5, R] {
    let ring = NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2))
    ring.lineWidth = 3
    rgb(0xffffff, 0.06).setStroke()
    ring.stroke()
}

// Sweep: leading edge at 52° (top right), trailing 80° behind it.
let lead: CGFloat = 52 * .pi / 180
let trail: CGFloat = 80 * .pi / 180
func sweepStrength(_ p: CGPoint) -> CGFloat {
    var a = atan2(p.y - c.y, p.x - c.x)
    var behind = lead - a
    while behind < 0 { behind += 2 * .pi }
    while behind > 2 * .pi { behind -= 2 * .pi }
    a = behind
    return a <= trail ? 1 - a / trail : 0
}

// Soft sweep glow under the dots.
ctx.saveGState()
let wedge = NSBezierPath()
wedge.move(to: c)
wedge.appendArc(withCenter: c, radius: R + 10, startAngle: (lead - trail) * 180 / .pi, endAngle: lead * 180 / .pi)
wedge.close()
wedge.addClip()
let glow = NSGradient(colors: [rgb(0x4f7cff, 0.0), rgb(0x4f7cff, 0.22)])!
glow.draw(fromCenter: c, radius: 0, toCenter: c, radius: R + 10, options: [])
ctx.restoreGState()

// Dot matrix.
let step: CGFloat = 53
for i in -6...6 {
    for j in -6...6 {
        let p = CGPoint(x: c.x + CGFloat(i) * step, y: c.y + CGFloat(j) * step)
        let d = hypot(p.x - c.x, p.y - c.y)
        if d > R - 20 || (i == 0 && j == 0) { continue }
        let s = sweepStrength(p)
        let base = 0.10 + 0.16 * (1 - d / R)
        let r: CGFloat = 10.5
        let rect = NSRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2)
        if s > 0 {
            rgb(0x6b92ff, base + 0.75 * s).setFill()
        } else {
            rgb(0xffffff, base).setFill()
        }
        NSBezierPath(roundedRect: rect, xRadius: 4.5, yRadius: 4.5).fill()
    }
}

func blip(_ i: Int, _ j: Int, _ color: NSColor, _ r: CGFloat) {
    let p = CGPoint(x: c.x + CGFloat(i) * step, y: c.y + CGFloat(j) * step)
    ctx.saveGState()
    ctx.setShadow(offset: .zero, blur: 30, color: color.withAlphaComponent(0.9).cgColor)
    color.setFill()
    NSBezierPath(roundedRect: NSRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2), xRadius: r * 0.42, yRadius: r * 0.42).fill()
    ctx.restoreGState()
}

blip(3, 4, rgb(0xff5f57), 19)    // only on this Mac
blip(-4, 2, rgb(0xffb547), 15)   // unsaved changes
blip(-2, -4, rgb(0x3ddc84), 13)  // backed up
blip(0, 0, rgb(0x6b92ff), 22)    // you

// Top-edge highlight.
let hl = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 184, yRadius: 184)
hl.lineWidth = 3
NSGradient(colors: [rgb(0xffffff, 0.18), rgb(0xffffff, 0.0)])!.draw(in: hl, angle: -90)

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
