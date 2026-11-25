// tests/startup_range_provenance.hlsl (#870): the consumer (ps_read) returns one element of the range its SRV
// window binds, indexed from the window's FirstElement, so the window alone decides which element is sampled
// and no filtering or rounding enters the readback. The producer (ps_write) stores one colour per pixel at
// write_base + the same index through a structured UAV and returns it, so the draw has an ordinary target too.
// RANGE_WIDTH must match range_elements in tests/startup_range_provenance.cpp.
#define RANGE_WIDTH 64

cbuffer Params : register(b0)
{
    float4 write_colour;  // ps_write: the colour stored into the range
    int write_base;       // ps_write: first element of the range, in elements
    int3 pad;
}
StructuredBuffer<float4> bound : register(t0);
RWStructuredBuffer<float4> target : register(u0);

float4 vs_main(uint id : SV_VertexID) : SV_Position
{
    return float4(id == 1 ? 3.0 : -1.0, id == 2 ? -3.0 : 1.0, 0.0, 1.0);
}

int element(float4 position : SV_Position)
{
    const int2 pixel = int2(position.xy);
    return pixel.y * RANGE_WIDTH + pixel.x;
}

float4 ps_read(float4 position : SV_Position) : SV_Target0
{
    return bound.Load(element(position));
}

float4 ps_write(float4 position : SV_Position) : SV_Target0
{
    target[write_base + element(position)] = write_colour;
    return write_colour;
}
