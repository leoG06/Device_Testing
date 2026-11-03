
distances = (input('Enter Your Distances(ex. "10,20,40") in microns: '))
distances = list(map(int,distances.split(",")))
print(distances)

for i in distances:
    print("Measuring i-th TLM")
    input = "no"
    while(input.lower() != "yes"):
    
        input = input(f"have you probed the {i} microns TLM? (yes/no)")
    
    #move to ith TLM
    '''
    measure the i-th TLM, which is gonna give us a csv file
    
    gotta figure out how to communicate with the machine
        use pyvisa and pyvisa-py(Message based communication, is the backend of pyvisa): pip install pyvisa pyvisa-py
        use gpib-ctypes(GPIB communication interface): pip install gpib-ctypes
    
    name of the machine HP 4145B Semiconductor Parameter Analyzer
    
    
    '''
#put the i-th TLM cleaned CSV data in TLM_IV-Sweep folder
    #format: "{distance in microns}.csv" "10.csv"

#run TLMDataAnalysis.ipynb for the analysis
#each CSV file in TLM_IV-Sweep corresponds to a specific distance

