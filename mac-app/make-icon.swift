// Draws Radar's 1024x1024 app icon: a dark squircle holding the letter R in a
// dot matrix, lit blue where a radar sweep passes over it, with one red blip. Usage: swift make-icon.swift out.png

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

// The artwork is drawn on a 64-unit grid (the same one as the dashboard's SVG
// logo, y pointing down) and mapped onto the 824pt body.
let unit = body.width / 64
func box(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat = 5) -> NSRect {
    NSRect(x: body.minX + x * unit, y: body.maxY - (y + w) * unit, width: w * unit, height: w * unit)
}
func dots(_ cells: [(CGFloat, CGFloat)], _ color: NSColor) {
    color.setFill()
    for (x, y) in cells { NSBezierPath(roundedRect: box(x, y), xRadius: 1.5 * unit, yRadius: 1.5 * unit).fill() }
}

// Sweep: a soft blue wedge from the centre, leading edge at 52°, 80° wide.
let c = CGPoint(x: body.midX, y: body.midY)
ctx.saveGState()
let wedge = NSBezierPath()
wedge.move(to: c)
wedge.appendArc(withCenter: c, radius: 26 * unit, startAngle: -28, endAngle: 52)
wedge.close()
wedge.addClip()
NSGradient(colors: [rgb(0x6b92ff, 0.04), rgb(0x6b92ff, 0.26)])!.draw(fromCenter: c, radius: 0, toCenter: c, radius: 26 * unit, options: [])
ctx.restoreGState()

// A 5x7 dot matrix spelling R. Cells the sweep passes over are blue.
dots([(22.5, 15.5), (29.5, 15.5), (36.5, 15.5), (22.5, 22.5), (29.5, 22.5), (22.5, 36.5), (36.5, 36.5),
      (22.5, 43.5), (29.5, 43.5), (43.5, 43.5), (22.5, 50.5), (29.5, 50.5), (36.5, 50.5)], rgb(0xffffff, 0.10))
dots([(36.5, 22.5), (43.5, 29.5), (43.5, 36.5)], rgb(0x6b92ff, 0.40))
dots([(15.5, 8.5), (22.5, 8.5), (29.5, 8.5), (36.5, 8.5), (15.5, 15.5), (15.5, 22.5), (15.5, 29.5), (22.5, 29.5),
      (29.5, 29.5), (15.5, 36.5), (29.5, 36.5), (15.5, 43.5), (36.5, 43.5), (15.5, 50.5), (43.5, 50.5)], rgb(0xf2f2f2))
dots([(43.5, 15.5), (43.5, 22.5), (36.5, 29.5)], rgb(0x6b92ff))

// The blip: a project that exists only on this computer.
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 34, color: rgb(0xff5f57, 0.85).cgColor)
rgb(0xff5f57).setFill()
NSBezierPath(roundedRect: box(42.5, 7.5, 7), xRadius: 2 * unit, yRadius: 2 * unit).fill()
ctx.restoreGState()

// Top-edge highlight.
let hl = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 184, yRadius: 184)
hl.lineWidth = 3
NSGradient(colors: [rgb(0xffffff, 0.18), rgb(0xffffff, 0.0)])!.draw(in: hl, angle: -90)

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
