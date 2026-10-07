// @@NAME@@: parpadeo de un LED a partir del reloj de la tarjeta (@@BOARD@@, @@MHZ@@ MHz).
// Con N = @@N@@ el LED cambia de estado cada ~0.34 s a 50 MHz (unos 1.5 Hz).
module @@NAME@@ #(parameter N = @@N@@) (
    input  wire clk,
    output wire led
);
    reg [N-1:0] cuenta = 0;

    always @(posedge clk)
        cuenta <= cuenta + 1'b1;

    assign led = cuenta[N-1];   // @@LEDNOTE@@
endmodule
